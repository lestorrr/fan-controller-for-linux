#!/usr/bin/env python3
"""
Unit tests for daemon/thinkpad-fan-controld against docs/CONTRACT.md §2-§13.

Run from the repository root:

    python3 -m unittest tests/test_daemon.py

Nothing here touches hardware, /run, /var, /etc or the sound system:

* the decision logic runs on the daemon's own fake clock and fake fan
  (FakeClock + SimHAL, the same classes --simulate uses);
* file-level code is pointed at temporary files by patching module globals;
* the control socket is exercised end to end twice: in-process (LiveDaemon
  loop in a thread, fake fan, socket in a temp dir) and against the real
  binary started as a subprocess with every FANCTL_* path override aimed at a
  temp dir and a small thread playing thinkpad_acpi on a fake fan file.

The daemon ignores the FANCTL_* overrides whenever it runs as root, so under
root these tests would reach the real /proc/acpi/ibm/fan: the whole module
refuses to run as root.  Temporary files go to $TMPDIR (tempfile's default).
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import itertools
import json
import math
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
DAEMON = REPO / "daemon" / "thinkpad-fan-controld"
T0 = 1_790_000_000.0                  # epoch origin of the fake clock

# Environment the daemon reads at import time or at run time.  Scrubbed for
# every load and every subprocess so nothing from the caller's shell (or from
# systemd, if the suite is ever run inside a unit) leaks into a test.
DAEMON_ENV_KEYS = (
    "RUNTIME_DIRECTORY", "STATE_DIRECTORY", "NOTIFY_SOCKET",
    "FANCTL_CONFIG_FILE", "FANCTL_LOG_FILE", "FANCTL_FAN_PROC", "FANCTL_HWMON_ROOT",
    "FANCTL_AC_ONLINE", "FANCTL_RUNTIME_DIR", "FANCTL_STATE_DIR",
    "FANCTL_GUI_UID", "FANCTL_GUI_GID", "FANCTL_GUI_HOME", "FANCTL_GUI_RUNTIME_DIR",
)

_load_counter = itertools.count()


def setUpModule():
    if os.geteuid() == 0:
        raise unittest.SkipTest("refusing to run as root: the daemon would use the real "
                                "/proc/acpi/ibm/fan and /etc paths")


def load_daemon(env=None):
    """
    Load the daemon script as a fresh module (it has no .py suffix, hence
    SourceFileLoader).  Its paths are resolved at import time, so `env` is
    applied only while the module body runs.  Bytecode is not written: the
    daemon directory is installed verbatim and must stay clean.
    """
    saved = {key: os.environ.pop(key, None) for key in DAEMON_ENV_KEYS}
    os.environ.update(env or {})
    old_flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        name = f"fan_controld_{next(_load_counter)}"
        loader = importlib.machinery.SourceFileLoader(name, str(DAEMON))
        spec = importlib.util.spec_from_loader(name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = old_flag
        for key in DAEMON_ENV_KEYS:
            os.environ.pop(key, None)
            if saved[key] is not None:
                os.environ[key] = saved[key]


D = load_daemon()

# The contract's §3 table, written out here on purpose rather than read from
# the daemon, so a drift in the daemon's own table is caught.
CONTRACT_INT_RANGES = {
    "sample_interval": (1, 5),
    "smoothing_up_s": (2, 30),
    "smoothing_down_s": (5, 120),
    "dwell_down_s": (0, 300),
    "step_down_spacing_s": (0, 60),
    "hysteresis": (0, 20),
    "critical_temp": (70, 105),
    "critical_exit_margin": (3, 20),
    "critical_exit_hold_s": (10, 600),
    "override_max_seconds": (60, 14400),
    "alert_cooldown": (30, 3600),
}
V2_CURVE = [{"temp": 0, "level": "auto"}, {"temp": 50, "level": "4"}, {"temp": 60, "level": "6"},
            {"temp": 70, "level": "7"}, {"temp": 80, "level": "disengaged", "down": 72}]
V2_BATTERY = [{"temp": 0, "level": "auto"}, {"temp": 60, "level": "3"}, {"temp": 70, "level": "5"},
              {"temp": 80, "level": "7"}, {"temp": 87, "level": "disengaged", "down": 78}]
V1_CURVE = [{"temp": 0, "level": "auto"}, {"temp": 50, "level": "4"}, {"temp": 60, "level": "6"},
            {"temp": 70, "level": "7"}, {"temp": 80, "level": "disengaged"}]
V1_BATTERY = [{"temp": 0, "level": "auto"}, {"temp": 60, "level": "3"}, {"temp": 70, "level": "5"},
              {"temp": 80, "level": "7"}, {"temp": 87, "level": "disengaged"}]
STATE_KEYS = {
    "schema", "version", "pid", "ts", "mono", "sample_interval", "sensor_ok", "temp_raw",
    "temp_fast", "temp_slow", "cpu", "igpu", "level", "level_proc", "rpm",
    "fan_control_available", "fan_status", "reason", "on_ac", "curve", "step_index", "steps",
    "curve_level", "up_at", "down_below", "level_since", "dwell_remaining_s", "critical",
    "override", "watchdog", "hysteresis", "critical_temp", "config_mtime", "config_schema",
    "rpm_by_level", "top_cycles_10min", "external_write_detected",
}


class TempDirCase(unittest.TestCase):
    """A private temp dir per test (under $TMPDIR), removed afterwards."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fanctl-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def write(self, name, text, mode=None):
        p = self.path(name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        if mode is not None:
            os.chmod(p, mode)
        return p


class Rig:
    """
    One Controller on the fake clock and fake fan.  step(raw) is one sample:
    the sensors read `raw` at the current time, the tick runs, then the clock
    advances one sample interval.  So after n steps the n-th tick happened at
    t = n - 1 and now() == n (with the default 1 s interval).
    """

    def __init__(self, cfg=None, *, ac=True, initial_level="auto"):
        self.clock = D.FakeClock(0.0, T0)
        self.hal = D.SimHAL(self.clock)
        self.hal.ac = ac
        self.log = D.Log(None, self.clock, collect=True)
        self.cfg, rejected = D.sanitize(cfg or {})
        if rejected:
            raise AssertionError(f"test config rejected: {rejected}")
        self.ctrl = D.Controller(self.cfg, self.hal, self.clock, self.log,
                                 initial_level=initial_level, record=True, pid=4242)
        self.ctrl.start()
        self.trace = []            # (t, raw, fast, slow, idx, level, reason)

    def now(self):
        return self.clock.monotonic()

    def step(self, raw, igpu=None, ac=None):
        self.hal.cpu = raw
        self.hal.igpu = igpu
        if ac is not None:
            self.hal.ac = ac
        t = self.now()
        self.ctrl.tick()
        c = self.ctrl
        self.trace.append((t, raw, c.t_fast, c.t_slow, c.idx, c.level, c.reason))
        self.clock.advance(c.dt())
        return c

    def run(self, raw, seconds):
        for _ in range(int(seconds)):
            self.step(raw)
        return self.ctrl

    def request(self, obj, uid=1000):
        return self.ctrl.handle_request(json.dumps(obj), uid)

    def lines(self, text):
        return [line for line in self.log.lines if text in line]

    def changes(self):
        """(t, from, to, reason) of every level change after the startup seat."""
        start = self.trace[0][0] if self.trace else 0.0
        return [(tr["t"], tr["from"], tr["to"], tr["reason"]) for tr in self.ctrl.transitions
                if tr["t"] > start]

    def idx_changes(self):
        out, prev = [], None
        for t, _raw, _f, _s, idx, _lvl, _r in self.trace:
            if prev is not None and idx != prev:
                out.append((t, prev, idx))
            prev = idx
        return out


# ── §3 sanitize: ranges and fallbacks ────────────────────────────────────────

class SanitizeRanges(unittest.TestCase):

    def test_defaults(self):
        cfg, rejected = D.sanitize({})
        self.assertEqual(rejected, [])
        self.assertEqual(cfg["schema"], 2)
        self.assertEqual(cfg["override_ceiling_temp"], 82)          # critical_temp - 8
        self.assertEqual(cfg["curve"], V2_CURVE)
        self.assertEqual(cfg["battery_curve"], V2_BATTERY)
        expected = {"sample_interval": 1, "smoothing_up_s": 8, "smoothing_down_s": 30,
                    "dwell_down_s": 45, "step_down_spacing_s": 10, "hysteresis": 6,
                    "critical_temp": 90, "critical_exit_margin": 8, "critical_exit_hold_s": 60,
                    "watchdog": 60, "override_max_seconds": 7200, "use_battery_curve": True,
                    "alerts_enabled": True, "alert_cooldown": 300,
                    "alert_sound": "/home/jhnlstrlclcn/Music/SYSTEM SOUND/90c.mp3"}
        for key, value in expected.items():
            self.assertEqual(cfg[key], value, key)
        self.assertEqual(set(cfg), set(expected) | {"schema", "override_ceiling_temp", "curve",
                                                    "battery_curve"})

    def test_int_ranges_edges_accepted_outside_rejected(self):
        for key, (lo, hi) in CONTRACT_INT_RANGES.items():
            with self.subTest(key=key):
                for good in (lo, hi):
                    cfg, rejected = D.sanitize({key: good})
                    self.assertEqual((cfg[key], rejected), (good, []))
                for bad in (lo - 1, hi + 1):
                    cfg, rejected = D.sanitize({key: bad})
                    self.assertEqual(rejected, [key])
                    self.assertEqual(cfg[key], D.DEFAULTS[key])       # no base -> default

    def test_invalid_value_falls_back_to_base_then_default(self):
        base = D.sanitize({"hysteresis": 9, "dwell_down_s": 120})[0]
        cfg, rejected = D.sanitize({"hysteresis": 99, "dwell_down_s": "60"}, base)
        self.assertEqual(sorted(rejected), ["dwell_down_s", "hysteresis"])
        self.assertEqual(cfg["hysteresis"], 9)
        self.assertEqual(cfg["dwell_down_s"], 120)
        # A missing key also keeps base; an invalid base value falls to DEFAULTS.
        cfg, rejected = D.sanitize({}, {"hysteresis": 12, "dwell_down_s": -1})
        self.assertEqual((cfg["hysteresis"], cfg["dwell_down_s"], rejected), (12, 45, []))

    def test_types(self):
        for bad in (True, "5", 2.5, None, [3], {"v": 3}):
            with self.subTest(value=bad):
                cfg, rejected = D.sanitize({"sample_interval": bad})
                self.assertEqual((cfg["sample_interval"], rejected), (1, ["sample_interval"]))
        # JSON has one number type: an integral float is the same integer.
        self.assertEqual(D.sanitize({"sample_interval": 3.0})[0]["sample_interval"], 3)
        for key in ("use_battery_curve", "alerts_enabled"):
            self.assertIs(D.sanitize({key: False})[0][key], False)
            cfg, rejected = D.sanitize({key: "no"})
            self.assertEqual((cfg[key], rejected), (True, [key]))

    def test_watchdog(self):
        self.assertEqual(D.sanitize({"watchdog": 0})[0]["watchdog"], 0)
        for bad in (1, 14, 121, -1):
            cfg, rejected = D.sanitize({"watchdog": bad})
            self.assertEqual((cfg["watchdog"], rejected), (60, ["watchdog"]), bad)
        # Raised to at least 3 * sample_interval + 15 (§3).
        self.assertEqual(D.sanitize({"watchdog": 15})[0]["watchdog"], 18)
        self.assertEqual(D.sanitize({"watchdog": 15, "sample_interval": 5})[0]["watchdog"], 30)
        self.assertEqual(D.sanitize({"watchdog": 120, "sample_interval": 5})[0]["watchdog"], 120)

    def test_override_ceiling_follows_critical(self):
        self.assertEqual(D.sanitize({"critical_temp": 95})[0]["override_ceiling_temp"], 87)
        self.assertEqual(D.sanitize({"override_ceiling_temp": 60})[0]["override_ceiling_temp"], 60)
        self.assertEqual(D.sanitize({"override_ceiling_temp": 89})[0]["override_ceiling_temp"], 89)
        for bad in (59, 90, 100, "80"):
            cfg, rejected = D.sanitize({"override_ceiling_temp": bad})
            self.assertEqual((cfg["override_ceiling_temp"], rejected), (82, ["override_ceiling_temp"]))
        # A base ceiling that no longer fits under a lower critical_temp falls
        # back to critical - 8 without being reported (the payload did not send it).
        base = D.sanitize({"override_ceiling_temp": 85})[0]
        cfg, rejected = D.sanitize({"critical_temp": 80}, base)
        self.assertEqual((cfg["override_ceiling_temp"], rejected), (72, []))

    def test_alert_sound(self):
        self.assertEqual(D.sanitize({"alert_sound": "/x/y.wav"})[0]["alert_sound"], "/x/y.wav")
        for bad in ("relative.wav", "/a\nb.wav", "/" + "a" * 400, 5, ""):
            cfg, rejected = D.sanitize({"alert_sound": bad})
            self.assertEqual(rejected, ["alert_sound"], repr(bad))
            self.assertEqual(cfg["alert_sound"], D.DEFAULTS["alert_sound"])
        self.assertEqual(D.sanitize({"alert_sound": "/" + "a" * 399})[1], [])

    def test_unknown_and_silent_keys(self):
        cfg, rejected = D.sanitize({"poll_interval": 3, "schema": 7, "bogus": 1, "curve": V2_CURVE})
        self.assertEqual(rejected, ["bogus"])
        self.assertNotIn("poll_interval", cfg)
        self.assertNotIn("bogus", cfg)
        self.assertEqual(cfg["schema"], 2)

    def test_inputs_not_mutated(self):
        raw = {"curve": [{"temp": 20, "level": "auto", "down": 5}] + V2_CURVE[1:], "watchdog": 15}
        snapshot = json.dumps(raw, sort_keys=True)
        cfg = D.sanitize(raw)[0]
        self.assertEqual(json.dumps(raw, sort_keys=True), snapshot)
        cfg["curve"][1]["temp"] = 51
        self.assertEqual(raw["curve"][1]["temp"], 50)


# ── §3 curve hard rules ──────────────────────────────────────────────────────

def curve(*steps):
    out = []
    for s in steps:
        temp, level = s[0], s[1]
        step = {"temp": temp, "level": level}
        if len(s) > 2:
            step["down"] = s[2]
        out.append(step)
    return out


class CurveRules(unittest.TestCase):

    def assertRejected(self, steps, why):
        self.assertIsNone(D.valid_curve(steps), why)
        base = D.sanitize({})[0]
        cfg, rejected = D.sanitize({"curve": steps, "battery_curve": steps}, base)
        self.assertEqual(sorted(rejected), ["battery_curve", "curve"], why)
        self.assertEqual(cfg["curve"], V2_CURVE, why)                 # previous curve kept
        self.assertEqual(cfg["battery_curve"], V2_BATTERY, why)

    def test_defaults_valid(self):
        self.assertEqual(D.valid_curve(V2_CURVE), V2_CURVE)
        self.assertEqual(D.valid_curve(V2_BATTERY), V2_BATTERY)

    def test_step_count(self):
        self.assertRejected([], "empty")
        eight = curve((0, "auto"), *[(10 * i + 20, str(i)) for i in range(1, 8)])
        self.assertIsNotNone(D.valid_curve(eight))
        self.assertRejected(eight + [{"temp": 100, "level": "disengaged"}], "nine steps")
        self.assertIsNotNone(D.valid_curve(curve((0, "7"))))        # a single step is fine
        self.assertRejected("not a list", "type")
        self.assertRejected([["0", "auto"]], "step not an object")

    def test_first_temp_forced_to_zero(self):
        got = D.valid_curve(curve((35, "auto"), (50, "4")))
        self.assertEqual(got[0]["temp"], 0)
        # ...and a `down` on the base step (never released from) is dropped.
        got = D.valid_curve([{"temp": 0, "level": "auto", "down": 3}, {"temp": 50, "level": "4"}])
        self.assertNotIn("down", got[0])

    def test_temps(self):
        self.assertRejected(curve((0, "auto"), (60, "4"), (60, "6")), "equal temps")
        self.assertRejected(curve((0, "auto"), (60, "4"), (55, "6")), "decreasing temps")
        self.assertRejected(curve((0, "auto"), (106, "7")), "above 105")
        self.assertIsNotNone(D.valid_curve(curve((0, "auto"), (105, "7"))))
        self.assertRejected(curve((0, "auto"), (-5, "3")), "negative")
        self.assertRejected(curve((0, "auto"), ("50", "4")), "string temp")
        self.assertRejected(curve((0, "auto"), (50.5, "4")), "fractional temp")

    def test_levels(self):
        self.assertRejected(curve((0, "auto"), (50, "8")), "unknown level")
        self.assertRejected(curve((0, "auto"), (50, 4)), "numeric level")
        self.assertRejected(curve((0, "3"), (50, "auto")), "auto above the base")
        self.assertIsNotNone(D.valid_curve(curve((0, "auto"), (50, "0"), (55, "full-speed"))))

    def test_rank_non_decreasing(self):
        self.assertRejected(curve((0, "auto"), (50, "6"), (55, "4")), "6 then 4")
        self.assertRejected(curve((0, "auto"), (50, "full-speed"), (55, "7")), "max then 7")
        ok = curve((0, "auto"), (50, "4"), (55, "4"), (70, "disengaged"), (80, "full-speed"))
        self.assertIsNotNone(D.valid_curve(ok))                       # equal ranks allowed

    def test_no_fan_off_when_warm(self):
        self.assertRejected(curve((0, "0"), (60, "0")), "level 0 at 60")
        self.assertRejected(curve((0, "0"), (59, "0"), (70, "0")), "level 0 at 70")
        self.assertIsNotNone(D.valid_curve(curve((0, "0"), (59, "0"), (60, "1"))))

    def test_down_range(self):
        self.assertIsNotNone(D.valid_curve(curve((0, "auto"), (80, "7", 60))))     # temp - 20
        self.assertIsNotNone(D.valid_curve(curve((0, "auto"), (80, "7", 79))))     # temp - 1
        self.assertRejected(curve((0, "auto"), (80, "7", 59)), "down below temp - 20")
        self.assertRejected(curve((0, "auto"), (80, "7", 80)), "down == temp")
        self.assertRejected(curve((0, "auto"), (80, "7", "72")), "string down")
        # An explicit null is the same as no `down`.
        self.assertEqual(D.valid_curve([{"temp": 0, "level": "auto"},
                                        {"temp": 80, "level": "7", "down": None}])[1],
                         {"temp": 80, "level": "7"})

    def test_default_release_is_temp_minus_hysteresis_at_decision_time(self):
        rig = Rig({"curve": curve((0, "auto"), (5, "3"), (50, "4"), (80, "7", 72))})
        self.assertEqual(rig.ctrl.release_temp(2), 44)              # 50 - 6, not stored
        self.assertEqual(rig.ctrl.release_temp(1), 0)               # max(0, 5 - 6)
        self.assertEqual(rig.ctrl.release_temp(3), 72)              # explicit down wins
        self.assertIsNone(rig.ctrl.release_temp(0))
        self.assertNotIn("down", rig.ctrl.cfg["curve"][2])
        rig.ctrl.cfg = D.sanitize({"hysteresis": 10, "curve": rig.ctrl.cfg["curve"]})[0]
        self.assertEqual(rig.ctrl.release_temp(2), 40)


# ── §3 v1 -> v2 migration ────────────────────────────────────────────────────

class Migration(unittest.TestCase):

    def test_migrate_v1(self):
        v1 = {"poll_interval": 3, "watchdog": 0, "hysteresis": 5, "curve": V1_CURVE,
              "battery_curve": V1_BATTERY, "critical_temp": 90}
        notes = []
        out = D.migrate_v1(v1, notes.append)
        self.assertNotIn("poll_interval", out)
        self.assertEqual(out["watchdog"], 60)
        self.assertEqual(notes, ["watchdog enabled (60 s) by upgrade"])
        self.assertEqual(out["curve"], V2_CURVE)
        self.assertEqual(out["battery_curve"], V2_BATTERY)
        self.assertEqual(out["schema"], 2)
        self.assertEqual(v1["watchdog"], 0)                          # input untouched
        self.assertEqual(v1["curve"], V1_CURVE)

    def test_customised_curve_and_watchdog_survive(self):
        custom = curve((0, "auto"), (55, "4"), (80, "disengaged"))
        notes = []
        out = D.migrate_v1({"watchdog": 90, "curve": custom}, notes.append)
        self.assertEqual((out["watchdog"], out["curve"], notes), (90, custom, []))
        cfg = D.config_from_disk({"watchdog": 90, "curve": custom}, None)
        self.assertEqual(cfg["curve"], custom)
        self.assertEqual(cfg["battery_curve"], V2_BATTERY)          # missing key added

    def test_v2_file_is_not_migrated(self):
        cfg = D.config_from_disk({"schema": 2, "watchdog": 0, "curve": V1_CURVE}, None)
        self.assertEqual((cfg["watchdog"], cfg["curve"]), (0, V1_CURVE))


# ── §3 load_config / --save-config ───────────────────────────────────────────

class ConfigFiles(TempDirCase):

    def setUp(self):
        super().setUp()
        self.cfg_path = self.path("etc", "config.json")
        self.log_path = self.path("daemon.log")
        for name, value in (("CONFIG_FILE", self.cfg_path), ("LOG_FILE", self.log_path)):
            patcher = mock.patch.object(D, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def save(self, payload):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        out, err = io.StringIO(), io.StringIO()
        rc = D.save_config_main(io.BytesIO(data), out, err)
        return rc, out.getvalue(), err.getvalue()

    def log_text(self):
        with open(self.log_path, encoding="utf-8") as f:
            return f.read()

    def test_load_config_migrates_a_v1_file(self):
        self.write("etc/config.json", json.dumps({"poll_interval": 3, "watchdog": 0, "curve": V1_CURVE}))
        log = D.Log(self.log_path)
        cfg, mtime, schema = D.load_config(log)
        self.assertEqual((cfg["watchdog"], cfg["curve"], schema), (60, V2_CURVE, 1))
        self.assertIsNotNone(mtime)
        self.assertEqual(self.log_text().count("watchdog enabled (60 s) by upgrade"), 1)

    def test_load_config_missing_or_broken_gives_defaults(self):
        log = D.Log(self.log_path)
        cfg, mtime, schema = D.load_config(log)
        self.assertEqual((cfg, mtime, schema), (D.sanitize({})[0], None, None))
        self.write("etc/config.json", "{not json")
        cfg, mtime, schema = D.load_config(log)
        self.assertEqual(cfg, D.sanitize({})[0])
        self.assertIn("using defaults", self.log_text())

    def test_save_first_install_and_single_json_line(self):
        rc, out, err = self.save({})
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1)
        reply = json.loads(lines[0])
        self.assertEqual(set(reply), {"config", "rejected"})
        self.assertEqual(reply["rejected"], [])
        with open(self.cfg_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), reply["config"])
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg_path).st_mode), 0o644)
        self.assertIn("Config updated from GUI", self.log_text())

    def test_save_partial_update_keeps_disk_and_reports_rejected(self):
        self.assertEqual(self.save({"hysteresis": 8, "dwell_down_s": 90})[0], 0)
        rc, out, _err = self.save({"hysteresis": 50, "battery_curve": curve((0, "auto"), (60, "0")),
                                   "mystery": 1, "alerts_enabled": False})
        self.assertEqual(rc, 0)
        reply = json.loads(out)
        self.assertEqual(sorted(reply["rejected"]), ["battery_curve", "hysteresis", "mystery"])
        cfg = reply["config"]
        self.assertEqual((cfg["hysteresis"], cfg["dwell_down_s"], cfg["alerts_enabled"]), (8, 90, False))
        self.assertEqual(cfg["battery_curve"], V2_BATTERY)
        self.assertNotIn("mystery", cfg)

    def test_save_migrates_a_v1_file_fed_back_by_the_installer(self):
        v1 = {"poll_interval": 3, "watchdog": 0, "curve": V1_CURVE, "battery_curve": V1_BATTERY}
        self.write("etc/config.json", json.dumps(v1))
        rc, out, _err = self.save(v1)
        self.assertEqual(rc, 0)
        cfg = json.loads(out)["config"]
        self.assertEqual((cfg["watchdog"], cfg["curve"], cfg["battery_curve"], cfg["schema"]),
                         (60, V2_CURVE, V2_BATTERY, 2))
        self.assertNotIn("poll_interval", cfg)
        self.assertEqual(json.loads(out)["rejected"], [])

    def test_save_does_not_migrate_a_v2_partial_update(self):
        # A v2 GUI sends {"watchdog": 0} ("Off"): that is a choice, not v1 data.
        self.assertEqual(self.save({})[0], 0)
        rc, out, _err = self.save({"watchdog": 0, "curve": V1_CURVE})
        cfg = json.loads(out)["config"]
        self.assertEqual((rc, cfg["watchdog"], cfg["curve"]), (0, 0, V1_CURVE))
        # ...but a payload carrying the v1-only poll_interval is an old GUI.
        rc, out, _err = self.save({"poll_interval": 3, "watchdog": 0})
        self.assertEqual((rc, json.loads(out)["config"]["watchdog"]), (0, 60))

    def test_save_bad_input(self):
        self.assertEqual(self.save({})[0], 0)
        before = os.stat(self.cfg_path).st_mtime_ns
        for payload in (b"{broken", b"[1, 2]", b"\xff\xfe", b"", b" " * 65537):
            with self.subTest(payload=payload[:10]):
                rc, out, err = self.save(payload)
                self.assertEqual((rc, out), (1, ""))
                self.assertTrue(err)
        self.assertEqual(os.stat(self.cfg_path).st_mtime_ns, before)
        # Exactly the limit is still read (and here happens to be valid JSON).
        body = b'{"hysteresis": 7}'
        rc, out, _err = self.save(body + b" " * (65536 - len(body)))
        self.assertEqual((rc, json.loads(out)["config"]["hysteresis"]), (0, 7))

    def test_save_config_subprocess_prints_exactly_one_line(self):
        env = {k: v for k, v in os.environ.items() if k not in DAEMON_ENV_KEYS}
        env.update(FANCTL_CONFIG_FILE=self.cfg_path, FANCTL_LOG_FILE=self.log_path,
                   PYTHONDONTWRITEBYTECODE="1")
        payload = json.dumps({"poll_interval": 3, "watchdog": 0, "curve": V1_CURVE, "x": 1})
        proc = subprocess.run([sys.executable, str(DAEMON), "--save-config"], input=payload,
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(proc.stdout.splitlines()), 1, proc.stdout)
        self.assertTrue(proc.stdout.endswith("\n"))
        reply = json.loads(proc.stdout)
        self.assertEqual(reply["rejected"], ["x"])
        self.assertEqual((reply["config"]["watchdog"], reply["config"]["curve"]), (60, V2_CURVE))
        self.assertIn("watchdog enabled (60 s) by upgrade", self.log_text())
        proc = subprocess.run([sys.executable, str(DAEMON), "--save-config"], input="nope",
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual((proc.returncode, proc.stdout), (1, ""))


# ── §4.6 curve walking: up / down / dwell / spacing / reseat ─────────────────

class CurveWalk(unittest.TestCase):

    def test_filters(self):
        rig = Rig()
        rig.step(60.0, igpu=58.5)
        self.assertEqual((rig.ctrl.t_fast, rig.ctrl.t_slow, rig.ctrl.raw), (60.0, 60.0, 60.0))
        rig.step(70.0, igpu=71.25)                    # raw = max(cpu, igpu), floats kept
        a_up, a_dn = 1 - math.exp(-1 / 8), 1 - math.exp(-1 / 30)
        self.assertAlmostEqual(rig.ctrl.t_fast, 60 + a_up * 11.25)
        self.assertAlmostEqual(rig.ctrl.t_slow, 60 + a_dn * 11.25)
        rig = Rig({"sample_interval": 2, "smoothing_up_s": 4, "smoothing_down_s": 10})
        rig.step(50.0)
        rig.step(60.0)
        self.assertAlmostEqual(rig.ctrl.t_fast, 50 + (1 - math.exp(-2 / 4)) * 10)
        self.assertAlmostEqual(rig.ctrl.t_slow, 50 + (1 - math.exp(-2 / 10)) * 10)

    def test_start_seats_by_first_sample(self):
        rig = Rig()
        rig.step(65.0)
        self.assertEqual((rig.ctrl.idx, rig.ctrl.level, rig.ctrl.reason), (2, "6", "curve"))
        rig = Rig()
        rig.step(85.0)
        self.assertEqual((rig.ctrl.idx, rig.ctrl.level), (4, "disengaged"))

    def test_up_one_step_per_tick(self):
        rig = Rig({"smoothing_up_s": 2})
        rig.step(40.0)
        for _ in range(4):
            rig.step(89.0)
        # T_fast passes 60 and 70 on the same tick, yet the index moves by one.
        self.assertEqual([i for _t, _a, i in rig.idx_changes()], [1, 2, 3, 4])
        self.assertEqual([t for t, _a, _i in rig.idx_changes()], [1, 2, 3, 4])
        fast_at_2 = rig.trace[2][2]
        self.assertGreaterEqual(fast_at_2, 70)
        self.assertEqual(rig.trace[2][4], 2)

    def test_up_on_first_tick_fast_reaches_threshold(self):
        rig = Rig()
        rig.step(45.0)
        rig.run(58.0, 30)
        up = [t for t, a, i in rig.idx_changes() if i > a]
        first = next(t for t, _r, fast, *_ in rig.trace if fast >= 50)
        self.assertEqual(up, [first])

    def test_down_waits_for_release_dwell_and_spacing(self):
        rig = Rig()
        rig.step(85.0)                      # seated at the top step at t = 0
        rig.run(50.0, 120)
        # T_slow = 50 + 35 exp(-t/30): below 72 from t = 14, but the dwell
        # since the seat (45 s) comes first; spacing puts the next DOWN at 55;
        # the third waits for T_slow < 54 (60 - 6), first true at t = 66;
        # step 1 (release 44) is never left at 50 C.
        downs = [(t, a, i) for t, a, i in rig.idx_changes() if i < a]
        self.assertEqual(downs, [(45.0, 4, 3), (55.0, 3, 2), (66.0, 2, 1)])
        self.assertEqual(rig.ctrl.level, "4")
        self.assertEqual([c[1:3] for c in rig.changes()],
                         [("disengaged", "7"), ("7", "6"), ("6", "4")])

    def test_dwell_counts_from_the_last_up(self):
        # A short falling-path time constant puts T_slow under the release
        # temperature (54) within 4 s of the drop, so only the dwell can hold
        # the DOWN back.
        rig = Rig({"smoothing_down_s": 5})
        rig.step(55.0)                      # step 1
        rig.run(66.0, 40)
        up = [t for t, a, i in rig.idx_changes() if i > a]
        self.assertEqual(len(up), 1)
        rig.run(40.0, 120)
        downs = [t for t, a, i in rig.idx_changes() if i < a]
        self.assertEqual(downs[0], up[0] + 45)

    def test_zero_dwell_and_spacing_still_one_step_per_tick(self):
        # The "Off" smoothing preset (2 s / 5 s): nothing but the release
        # temperatures gate the DOWN steps, and still only one per tick.
        rig = Rig({"dwell_down_s": 0, "step_down_spacing_s": 0,
                   "smoothing_up_s": 2, "smoothing_down_s": 5})
        rig.step(85.0)
        rig.run(30.0, 60)
        moves = rig.idx_changes()
        self.assertEqual([(a, i) for _t, a, i in moves], [(4, 3), (3, 2), (2, 1), (1, 0)])
        times = [t for t, _a, _i in moves]
        self.assertEqual(len(set(times)), len(times))

    def test_reseat_on_config_reload_keeps_emas_and_restarts_dwell(self):
        rig = Rig()
        rig.step(75.0)
        rig.run(75.0, 10)
        fast, slow = rig.ctrl.t_fast, rig.ctrl.t_slow
        new_curve = curve((0, "auto"), (40, "3"), (55, "5"), (74, "6"), (90, "7"))
        rig.hal.cfg_raw = {"schema": 2, "curve": new_curve}
        rig.hal.cfg_mtime = 1234.5
        t_reload = rig.now()
        rig.step(75.0)
        c = rig.ctrl
        self.assertEqual(c.cfg["curve"], new_curve)
        self.assertEqual((c.idx, c.level), (3, "6"))
        self.assertEqual((c.last_up, c.last_change), (t_reload, t_reload))
        a_up, a_dn = 1 - math.exp(-1 / 8), 1 - math.exp(-1 / 30)
        self.assertAlmostEqual(c.t_fast, fast + a_up * (75 - fast))   # not re-seeded
        self.assertAlmostEqual(c.t_slow, slow + a_dn * (75 - slow))
        self.assertEqual(len(rig.lines("Config reloaded")), 1)
        self.assertEqual(c.state()["config_mtime"], 1234.5)
        # Same content under a new mtime: nothing to do, nothing logged.
        rig.hal.cfg_mtime = 1300.0
        rig.step(75.0)
        self.assertEqual(len(rig.lines("Config reloaded")), 1)

    def test_broken_reload_keeps_running_config_and_logs_once(self):
        rig = Rig({"hysteresis": 9})
        rig.step(60.0)
        rig.hal.cfg_raw, rig.hal.cfg_mtime, rig.hal.cfg_error = None, 50.0, "invalid JSON: x"
        rig.run(60.0, 5)
        self.assertEqual(rig.ctrl.cfg["hysteresis"], 9)
        self.assertEqual(len(rig.lines("config reload failed")), 1)
        rig.hal.cfg_mtime = 51.0                       # another bad write: logged again
        rig.run(60.0, 3)
        self.assertEqual(len(rig.lines("config reload failed")), 2)
        # A valid file is sanitized over the running config (invalid keys keep it).
        rig.hal.cfg_raw, rig.hal.cfg_mtime, rig.hal.cfg_error = (
            {"schema": 2, "hysteresis": 99, "dwell_down_s": 30}, 52.0, None)
        rig.step(60.0)
        self.assertEqual((rig.ctrl.cfg["hysteresis"], rig.ctrl.cfg["dwell_down_s"]), (9, 30))

    def test_reload_keeps_an_override(self):
        rig = Rig()
        rig.step(60.0)
        self.assertTrue(rig.request({"cmd": "hold", "level": "5", "seconds": 600})["ok"])
        rig.hal.cfg_raw, rig.hal.cfg_mtime = {"schema": 2, "hysteresis": 4}, 77.0
        rig.run(60.0, 3)
        self.assertEqual((rig.ctrl.cfg["hysteresis"], rig.ctrl.level, rig.ctrl.reason), (4, "5", "override"))

# ── §4.5 critical: entry, alerts, amended exit ───────────────────────────────

def hhmmss(mono):
    return time.strftime("%H:%M:%S", time.localtime(T0 + mono))


class Critical(unittest.TestCase):

    def test_entry_needs_two_consecutive_samples(self):
        rig = Rig()
        rig.run(80.0, 5)
        for raw in (91.0, 89.0, 92.0):
            rig.step(raw)
            self.assertFalse(rig.ctrl.critical, raw)
        t = rig.now()
        rig.step(90.0)                                   # second consecutive >= 90
        c = rig.ctrl
        self.assertTrue(c.critical)
        self.assertEqual((c.level, c.reason, c.critical_since), ("disengaged", "critical", T0 + t))
        self.assertEqual(len(rig.lines("CRITICAL entered (raw 90.0")), 1)

    def test_plus_three_enters_on_one_sample(self):
        rig = Rig()
        rig.run(80.0, 5)
        rig.step(93.0)
        self.assertTrue(rig.ctrl.critical)

    def test_sensor_gap_breaks_the_pair(self):
        rig = Rig()
        rig.run(80.0, 5)
        rig.step(91.0)
        rig.step(None)
        rig.step(91.0)
        self.assertFalse(rig.ctrl.critical)
        rig.step(91.0)
        self.assertTrue(rig.ctrl.critical)

    def test_alert_on_entry_and_every_cooldown_with_amended_wording(self):
        rig = Rig()
        rig.run(80.0, 5)
        t_entry = rig.now() + 1                          # 92 < 93: the SECOND sample enters
        rig.run(92.0, 700)                               # T_slow never gets below 82
        self.assertTrue(rig.ctrl.critical)
        self.assertEqual([t for t, _p in rig.hal.alerts], [t_entry, t_entry + 300, t_entry + 600])
        self.assertEqual({p for _t, p in rig.hal.alerts}, {D.DEFAULTS["alert_sound"]})
        alerts = rig.lines("ALERT:")
        self.assertEqual(len(alerts), 3)
        for line in alerts:
            self.assertRegex(line, r"ALERT: critical since " + hhmmss(t_entry)
                             + r" \(raw \d+\.\d, slow \d+\.\d\)$")
        self.assertFalse(any("at or above" in line for line in rig.log.lines))
        state = rig.ctrl.state()["critical"]
        self.assertEqual(state, {"active": True, "since": T0 + t_entry, "alerts": 3})
        kinds = [e[1] for e in rig.ctrl.events]
        self.assertEqual((kinds.count("critical_on"), kinds.count("alert")), (1, 3))

    def test_alerts_disabled(self):
        rig = Rig({"alerts_enabled": False})
        rig.run(80.0, 3)
        rig.run(95.0, 400)
        self.assertTrue(rig.ctrl.critical)
        self.assertEqual((rig.hal.alerts, rig.lines("ALERT:")), ([], []))

    def test_exit_after_hold_onto_top_step_then_normal_down_rules(self):
        rig = Rig()
        rig.run(80.0, 5)
        t_entry = rig.now()
        rig.run(95.0, 2)
        rig.run(60.0, 200)
        cleared = rig.lines("CRITICAL cleared")
        self.assertEqual(len(cleared), 1)
        self.assertRegex(cleared[0], r"CRITICAL cleared \(slow \d+\.\d\d < 82\) -> top step")
        t_exit = next(e[0] for e in rig.ctrl.events if e[1] == "critical_off") - T0
        # T_slow was below 82 long before: the 60 s hold is what ended it.
        self.assertEqual(t_exit, t_entry + 60)
        after = [c for c in rig.changes() if c[0] >= t_exit]
        # Leaves onto the TOP step (disengaged: no change at the exit itself);
        # the first DOWN comes a full dwell later, then spacing / release rules.
        self.assertEqual(after[0][:3], (t_exit + 45, "disengaged", "7"))
        self.assertEqual(after[0][3], "curve")
        self.assertEqual(after[1][:3], (t_exit + 55, "7", "6"))

    def test_exit_state_is_top_step_not_reseat(self):
        top7 = curve((0, "auto"), (50, "4"), (60, "6"), (80, "7"))
        rig = Rig({"curve": top7, "critical_exit_hold_s": 10})
        rig.run(70.0, 5)
        rig.run(95.0, 2)
        self.assertEqual(rig.ctrl.level, "disengaged")
        rig.run(60.0, 30)
        c = rig.ctrl
        t_exit = next(e[0] for e in c.events if e[1] == "critical_off") - T0
        exit_tick = next(tr for tr in rig.trace if tr[0] == t_exit)
        self.assertLess(exit_tick[2], 80)          # a T_fast reseat would not pick the top
        self.assertEqual(exit_tick[4:], (3, "7", "curve"))
        self.assertEqual((c.last_up, c.last_change), (t_exit, t_exit))   # dwell starts at the exit
        self.assertIn((t_exit, "disengaged", "7", "curve"), rig.changes())

    def test_no_exit_while_slow_is_hot(self):
        rig = Rig()
        rig.run(80.0, 3)
        rig.run(95.0, 2)
        rig.run(86.0, 600)
        self.assertTrue(rig.ctrl.critical)
        self.assertEqual(rig.lines("CRITICAL cleared"), [])

    def test_cooldown_spans_episodes(self):
        rig = Rig({"critical_exit_hold_s": 10, "critical_exit_margin": 20})
        rig.run(60.0, 3)
        t1 = rig.now()
        rig.run(95.0, 2)
        rig.run(40.0, 60)                                  # exits (slow < 70 after 10 s)
        self.assertFalse(rig.ctrl.critical)
        t2 = rig.now()
        rig.run(95.0, 2)                                   # re-entry within 300 s
        self.assertTrue(rig.ctrl.critical)
        self.assertEqual([t for t, _p in rig.hal.alerts], [t1])
        rig.run(95.0, 300)
        self.assertEqual([t for t, _p in rig.hal.alerts], [t1, t1 + 300])
        self.assertGreater(t2, t1)


# ── §4.7 / §7 hold (override) envelope ───────────────────────────────────────

class Override(unittest.TestCase):

    def test_hold_applies_before_the_reply(self):
        rig = Rig()
        rig.run(60.0, 3)
        t = rig.now()
        reply = rig.request({"cmd": "hold", "level": "3", "seconds": 600}, uid=1000)
        self.assertTrue(reply["ok"])
        self.assertEqual(rig.hal.level, "3")                         # written already
        st = reply["state"]
        self.assertEqual((st["level"], st["reason"]), ("3", "override"))
        self.assertEqual(st["override"], {"level": "3", "until": T0 + t + 600, "set_at": T0 + t,
                                          "remaining_s": 600, "suspended": False})
        self.assertEqual(len(rig.lines("Override 3 for 600s (uid 1000)")), 1)
        self.assertIn(["override_start", "3 600s"], [e[1:] for e in rig.ctrl.events])

    def test_seconds_zero_means_max_and_max_clamps(self):
        rig = Rig({"override_max_seconds": 3600})
        rig.step(60.0)
        for seconds, expected in ((0, 3600), (99999, 3600), (120, 120), (3600, 3600)):
            st = rig.request({"cmd": "hold", "level": "5", "seconds": seconds})["state"]
            self.assertEqual(st["override"]["remaining_s"], expected, seconds)
        st = rig.request({"cmd": "hold", "level": "5"})["state"]      # no seconds -> 0 -> max
        self.assertEqual(st["override"]["remaining_s"], 3600)

    def test_bad_requests(self):
        rig = Rig()
        rig.step(60.0)
        for req in ({"cmd": "hold", "level": "8"}, {"cmd": "hold", "level": 3},
                    {"cmd": "hold", "level": "3", "seconds": -1},
                    {"cmd": "hold", "level": "3", "seconds": "60"},
                    {"cmd": "hold", "level": "3", "seconds": True},
                    {"cmd": "dance"}, {"nocmd": 1}):
            reply = rig.request(req)
            self.assertEqual(set(reply), {"ok", "error", "message"}, req)
            self.assertEqual((reply["ok"], reply["error"]), (False, "bad_request"), req)
        for raw in ("not json", "[1]", b"\xff"):
            self.assertEqual(rig.ctrl.handle_request(raw, 1000)["error"], "bad_request")
        self.assertIsNone(rig.ctrl.override)

    def test_fan_off_refused_when_hot_or_unknown(self):
        rig = Rig()
        rig.run(55.0, 2)
        reply = rig.request({"cmd": "hold", "level": "0", "seconds": 60})
        self.assertEqual((reply["ok"], reply["error"]), (False, "too_hot_for_fan_off"))
        rig.run(None, 1)                                    # temperature unknown
        self.assertEqual(rig.request({"cmd": "hold", "level": "0"})["error"], "too_hot_for_fan_off")
        rig.run(54.9, 1)
        reply = rig.request({"cmd": "hold", "level": "0", "seconds": 60})
        self.assertTrue(reply["ok"])
        self.assertEqual(rig.hal.level, "0")

    def test_fan_control_unavailable_only_from_the_commands_line(self):
        rig = Rig()
        rig.run(50.0, 2)
        # Fan stopped at level 0: /proc says "status: disabled" -- still controllable.
        self.assertTrue(rig.request({"cmd": "hold", "level": "0", "seconds": 60})["ok"])
        self.assertEqual(rig.hal.fan_read()["status"], "disabled")
        reply = rig.request({"cmd": "hold", "level": "4", "seconds": 60})
        self.assertTrue(reply["ok"])
        self.assertIs(reply["state"]["fan_control_available"], True)
        # No "commands: level" line (fan_control=1 not active): refused.
        rig.hal.control_available = False
        reply = rig.request({"cmd": "hold", "level": "5", "seconds": 60})
        self.assertEqual((reply["ok"], reply["error"]), (False, "fan_control_unavailable"))
        self.assertEqual(rig.ctrl.override["level"], "4")

    def test_single_spike_does_not_suspend(self):
        rig = Rig()
        rig.run(75.0, 30)
        rig.request({"cmd": "hold", "level": "3", "seconds": 3600})
        rig.step(92.0)                                      # one Tctl spike (not critical)
        rig.run(75.0, 10)
        self.assertFalse(rig.ctrl.override["suspended"])
        self.assertTrue(all(tr[5:] == ("3", "override") for tr in rig.trace[30:]))
        self.assertGreaterEqual(max(tr[1] for tr in rig.trace[30:]), 85)

    def test_suspend_on_fast_resume_on_slow_after_30s(self):
        rig = Rig({"smoothing_down_s": 5})
        rig.run(75.0, 30)
        rig.request({"cmd": "hold", "level": "3", "seconds": 3600})
        rig.run(86.0, 12)                                   # T_fast crosses 82 at t = 38
        c = rig.ctrl
        self.assertTrue(c.override["suspended"])
        i = next(k for k, tr in enumerate(rig.trace) if tr[6] == "override_suspended_hot")
        self.assertGreaterEqual(rig.trace[i][2], 82)          # T_fast >= ceiling there
        self.assertLess(rig.trace[i - 1][2], 82)              # and not one tick earlier
        self.assertNotEqual(rig.trace[i][5], "3")             # the curve's level applies
        t_susp = rig.trace[i][0]
        rig.run(50.0, 60)
        j = next(k for k, tr in enumerate(rig.trace) if k > i and tr[6] == "override")
        # With the 5 s falling-path constant T_slow is below 76 within a few
        # seconds, so the 30 s minimum decides when the hold comes back.
        self.assertEqual(rig.trace[j][0], t_susp + 30)
        self.assertLess(rig.trace[j][3], 76)
        self.assertEqual(rig.trace[j][5], "3")
        self.assertEqual(len(rig.lines("Override 3 suspended (fast")), 1)
        self.assertEqual(len(rig.lines("Override 3 resumed (slow")), 1)

    def test_resume_waits_for_slow_below_ceiling_minus_hysteresis(self):
        rig = Rig()
        rig.run(75.0, 30)
        rig.request({"cmd": "hold", "level": "3", "seconds": 3600})
        rig.run(86.0, 40)
        rig.run(70.0, 300)
        k = next(k for k, tr in enumerate(rig.trace) if k > 70 and tr[6] == "override")
        self.assertLess(rig.trace[k][3], 76)
        self.assertGreaterEqual(rig.trace[k - 1][3], 76)       # first tick it was allowed

    def test_auto_hold_is_never_suspended(self):
        rig = Rig()
        rig.run(75.0, 5)
        rig.request({"cmd": "hold", "level": "auto", "seconds": 3600})
        rig.run(88.0, 60)
        self.assertFalse(rig.ctrl.override["suspended"])
        self.assertEqual((rig.ctrl.level, rig.ctrl.reason), ("auto", "override"))

    def test_critical_wins_and_the_hold_comes_back_only_when_cool(self):
        rig = Rig()
        rig.run(75.0, 5)
        rig.request({"cmd": "hold", "level": "3", "seconds": 7200})
        rig.run(95.0, 120)
        c = rig.ctrl
        self.assertEqual((c.level, c.reason, c.override["level"]), ("disengaged", "critical", "3"))
        self.assertTrue(c.override["suspended"])             # suspended during critical too
        # 78 C: T_slow falls under 82 (critical exits) but never under 76, so
        # the hold must stay suspended: the low level does not come straight back.
        rig.run(78.0, 200)
        self.assertFalse(c.critical)
        self.assertEqual((c.level, c.reason), ("disengaged", "override_suspended_hot"))
        t_exit = next(e[0] for e in c.events if e[1] == "critical_off") - T0
        after_exit = [tr for tr in rig.trace if tr[0] >= t_exit]
        self.assertTrue(all(tr[6] == "override_suspended_hot" for tr in after_exit))
        rig.run(60.0, 120)
        self.assertEqual((c.level, c.reason, c.override["suspended"]), ("3", "override", False))
        k = next(k for k, tr in enumerate(rig.trace) if tr[0] > t_exit and tr[6] == "override")
        self.assertLess(rig.trace[k][3], 76)

    def test_expiry_reseats(self):
        rig = Rig()
        rig.run(65.0, 5)
        t = rig.now()
        rig.request({"cmd": "hold", "level": "7", "seconds": 60})
        rig.run(65.0, 70)
        c = rig.ctrl
        self.assertIsNone(c.override)
        self.assertEqual(len(rig.lines("Override 7 ended (expired)")), 1)
        exp = next(e for e in c.events if e[1] == "override_end")
        self.assertEqual((exp[0], exp[2]), (T0 + t + 60, "7 expired"))
        tick = next(tr for tr in rig.trace if tr[0] == t + 60)
        self.assertEqual(tick[4:], (2, "6", "curve"))             # re-seated by T_fast (65)
        self.assertEqual(c.last_up, t + 60)

    def test_resume_command(self):
        rig = Rig()
        rig.run(65.0, 5)
        self.assertTrue(rig.request({"cmd": "resume"})["ok"])     # no hold: still ok
        rig.request({"cmd": "hold", "level": "2", "seconds": 600})
        reply = rig.request({"cmd": "resume"}, uid=0)
        self.assertTrue(reply["ok"])
        self.assertIsNone(reply["state"]["override"])
        self.assertEqual((rig.hal.level, reply["state"]["reason"]), ("6", "curve"))
        self.assertEqual(len(rig.lines("Override 2 ended (resumed by uid 0)")), 1)

    def test_test_alert_rate_limit(self):
        rig = Rig({"alerts_enabled": False})
        rig.step(50.0)
        self.assertEqual(rig.request({"cmd": "test_alert"}), {"ok": True})
        rig.run(50.0, 5)
        self.assertEqual(rig.request({"cmd": "test_alert"})["error"], "rate_limited")
        rig.run(50.0, 5)
        self.assertEqual(rig.request({"cmd": "test_alert"}), {"ok": True})
        self.assertEqual(len(rig.hal.alerts), 2)

    def test_status(self):
        rig = Rig()
        rig.step(50.0)
        reply = rig.request({"cmd": "status"})
        self.assertEqual((reply["ok"], set(reply["state"])), (True, STATE_KEYS))


# ── §4.1 sensor loss ─────────────────────────────────────────────────────────

class SensorLoss(unittest.TestCase):

    def test_auto_after_three_misses_logged_once(self):
        rig = Rig()
        rig.run(75.0, 10)
        rig.run(None, 2)
        self.assertEqual((rig.ctrl.level, rig.ctrl.reason, rig.ctrl.sensor_lost), ("7", "curve", False))
        rig.step(None)
        self.assertEqual((rig.ctrl.level, rig.ctrl.reason, rig.ctrl.sensor_lost), ("auto", "sensor_lost", True))
        rig.run(None, 30)
        self.assertEqual(len(rig.lines("no valid temperature")), 1)
        self.assertEqual([e[1] for e in rig.ctrl.events].count("sensor_lost"), 1)
        st = rig.ctrl.state()
        self.assertEqual((st["sensor_ok"], st["temp_raw"], st["reason"]), (False, None, "sensor_lost"))

    def test_invalid_readings_are_missing(self):
        rig = Rig()
        rig.run(70.0, 2)
        for bad in (0.0, 150.0, -3.0, 400.0):
            rig.step(bad)
        self.assertTrue(rig.ctrl.sensor_lost)

    def test_one_sensor_is_enough(self):
        rig = Rig()
        rig.run(None, 1)
        rig.step(None, igpu=70.0)
        self.assertEqual((rig.ctrl.raw, rig.ctrl.sensor_lost), (70.0, False))

    def test_disengaged_when_lost_during_critical(self):
        rig = Rig()
        rig.run(80.0, 2)
        rig.run(95.0, 2)
        rig.run(None, 3)
        self.assertEqual((rig.ctrl.level, rig.ctrl.reason), ("disengaged", "sensor_lost"))

    def test_recovery_reseeds_and_reseats(self):
        rig = Rig()
        rig.run(80.0, 10)
        rig.run(None, 5)
        t = rig.now()
        rig.step(66.0)
        c = rig.ctrl
        self.assertEqual((c.t_fast, c.t_slow, c.idx, c.level, c.reason), (66.0, 66.0, 2, "6", "curve"))
        self.assertEqual((c.last_up, c.sensor_lost), (t, False))
        self.assertEqual(len(rig.lines("Temperature sensors back (raw 66.0)")), 1)
        self.assertEqual([e[1] for e in c.events].count("sensor_ok"), 1)

    def test_loss_before_the_first_sample(self):
        rig = Rig()
        rig.run(None, 4)
        self.assertEqual((rig.ctrl.level, rig.ctrl.reason), ("auto", "sensor_lost"))
        rig.step(72.0)
        c = rig.ctrl
        self.assertEqual((c.sensor_lost, c.idx, c.level, c.reason), (False, 3, "7", "curve"))
        self.assertEqual(len(rig.lines("Temperature sensors back")), 1)
        rig.run(72.0, 3)
        self.assertEqual(len(rig.lines("Temperature sensors back")), 1)

# ── §4.3 power source debounce ───────────────────────────────────────────────

class PowerSource(TempDirCase):

    def test_three_identical_readings_switch_bounces_do_not(self):
        rig = Rig()
        rig.run(65.0, 5)                                        # AC curve: 60 -> "6"
        for pattern in ([False, True], [False, False, True]):  # 1- and 2-sample bounces
            for ac in pattern:
                rig.step(65.0, ac=ac)
        self.assertEqual((rig.ctrl.curve_name(), rig.lines("Power ->")), ("ac", []))
        n_changes = len(rig.changes())
        rig.step(65.0, ac=False)
        rig.step(65.0, ac=False)
        self.assertEqual(rig.ctrl.curve_name(), "ac")
        t = rig.now()
        rig.step(65.0, ac=False)                                # third identical reading
        c = rig.ctrl
        self.assertEqual((c.curve_name(), c.on_ac, c.idx, c.level), ("battery", False, 1, "3"))
        self.assertEqual(rig.lines("Power -> battery")[0][22:], "Power -> battery")
        self.assertEqual(rig.changes()[n_changes:], [(t, "6", "3", "curve")])
        self.assertEqual((c.last_up, c.last_change), (t, t))    # re-seated
        self.assertEqual([e[1:] for e in c.events if e[1] == "ac"], [["ac", "battery"]])

    def test_battery_curve_disabled(self):
        rig = Rig({"use_battery_curve": False})
        rig.run(65.0, 5)
        for _ in range(4):
            rig.step(65.0, ac=False)
        self.assertEqual(len(rig.lines("Power -> battery")), 1)
        self.assertEqual((rig.ctrl.curve_name(), rig.ctrl.level, rig.changes()), ("ac", "6", []))

    def test_initial_reading_needs_no_debounce(self):
        rig = Rig(ac=False)
        rig.step(65.0)
        self.assertEqual((rig.ctrl.curve_name(), rig.ctrl.level), ("battery", "3"))

    def test_live_read_ac(self):
        hal = D.LiveHAL(D.Log(None))
        with mock.patch.object(D, "AC_ONLINE", self.path("missing")):
            self.assertIs(hal.read_ac(), True)                  # missing node -> AC
        for text, expected in (("0\n", False), ("1\n", True)):
            p = self.write("online", text)
            with mock.patch.object(D, "AC_ONLINE", p):
                self.assertIs(hal.read_ac(), expected)


# ── §4.9 external writes / §4.10 watchdog / refresh ──────────────────────────

class FanWrites(unittest.TestCase):

    def test_external_write_detected_once_and_reasserted(self):
        rig = Rig()
        rig.run(75.0, 5)
        rig.hal.level = "5"                                     # someone else wrote the EC
        rig.step(75.0)
        self.assertEqual(rig.hal.level, "7")
        self.assertEqual(len(rig.lines("external fan write detected (level 5)")), 1)
        self.assertTrue(rig.ctrl.state()["external_write_detected"])
        rig.hal.level = "4"
        rig.step(75.0)
        self.assertEqual(len(rig.lines("external fan write detected")), 1)   # same episode
        rig.run(75.0, 60)
        self.assertFalse(rig.ctrl.state()["external_write_detected"])
        rig.hal.level = "3"
        rig.step(75.0)
        self.assertEqual(len(rig.lines("external fan write detected")), 2)

    def test_watchdog_revert_to_auto_is_silent(self):
        rig = Rig()
        rig.run(75.0, 5)
        rig.hal.level = "auto"
        rig.step(75.0)
        self.assertEqual(rig.hal.level, "7")
        self.assertEqual(rig.lines("external"), [])
        self.assertFalse(rig.ctrl.state()["external_write_detected"])

    def test_full_speed_reads_back_as_disengaged(self):
        rig = Rig({"curve": curve((0, "auto"), (60, "6"), (80, "full-speed"))})
        rig.run(85.0, 30)
        self.assertEqual((rig.ctrl.level, rig.ctrl.level_proc), ("full-speed", "disengaged"))
        self.assertEqual(rig.lines("external"), [])

    def test_watchdog_armed_first(self):
        rig = Rig()
        self.assertEqual(rig.hal.writes[0][1], "watchdog 60")
        self.assertEqual(rig.hal.watchdog, 60)
        self.assertEqual(Rig({"watchdog": 0}).hal.writes[0][1], "watchdog 0")

    def test_refresh_interval(self):
        for wd, si, expected in ((60, 1, 20), (0, 1, 30), (30, 1, 10), (120, 1, 20),
                                 (45, 1, 15), (15, 5, 10), (15, 1, 6)):
            with self.subTest(watchdog=wd, sample=si):
                self.assertEqual(Rig({"watchdog": wd, "sample_interval": si}).ctrl.refresh_interval(),
                                 expected)

    def test_unchanged_level_rewritten_on_the_refresh_interval_only(self):
        for wd, period in ((60, 20), (0, 30)):
            rig = Rig({"watchdog": wd})
            rig.run(65.0, 101)
            writes = [(t, cmd) for t, cmd in rig.hal.writes if cmd.startswith("level ")]
            self.assertEqual(writes, [(float(t), "level 6") for t in range(0, 101, period)])
            self.assertEqual(len(rig.lines("Fan ")), 1)          # refreshes are never logged

    def test_reload_rearms_watchdog_only_when_it_changes(self):
        rig = Rig()
        rig.run(65.0, 3)
        rig.hal.cfg_raw, rig.hal.cfg_mtime = {"schema": 2, "watchdog": 90}, 10.0
        rig.step(65.0)
        rig.hal.cfg_raw, rig.hal.cfg_mtime = {"schema": 2, "watchdog": 90, "hysteresis": 4}, 11.0
        rig.step(65.0)
        wd = [cmd for _t, cmd in rig.hal.writes if cmd.startswith("watchdog ")]
        self.assertEqual(wd, ["watchdog 60", "watchdog 90"])
        self.assertEqual(len(rig.lines("EC watchdog set to 90 s")), 1)
        self.assertEqual(rig.ctrl.refresh_interval(), 20)

    def test_failed_write_is_retried_and_level_not_claimed(self):
        rig = Rig()
        rig.run(65.0, 3)
        rig.hal.write_ok = False
        rig.run(75.0, 20)
        self.assertEqual(rig.ctrl.level, "6")                   # never claimed "7"
        rig.hal.write_ok = True
        rig.step(75.0)
        self.assertEqual((rig.ctrl.level, rig.hal.level), ("7", "7"))

    def test_shutdown_order(self):
        rig = Rig()
        rig.run(70.0, 3)
        rig.ctrl.shutdown()
        self.assertEqual([cmd for _t, cmd in rig.hal.writes[-2:]], ["watchdog 0", "level auto"])
        self.assertEqual(len(rig.lines("Daemon stopping — returning fan to firmware control")), 1)


# ── §4.11-§4.13 / §5 / §6 publication ────────────────────────────────────────

class Publication(TempDirCase):

    def test_state_fields(self):
        rig = Rig()
        rig.run(75.0, 3)
        st = rig.ctrl.state()
        self.assertEqual(set(st), STATE_KEYS)
        expected = {"schema": 1, "version": "2.0.0", "pid": 4242, "ts": T0 + 3, "mono": 3.0,
                    "sample_interval": 1, "sensor_ok": True, "temp_raw": 75.0, "cpu": 75.0,
                    "igpu": None, "level": "7", "level_proc": "7", "rpm": 4000,
                    "fan_control_available": True, "fan_status": "enabled", "reason": "curve",
                    "on_ac": True, "curve": "ac", "step_index": 3, "steps": 5, "curve_level": "7",
                    "up_at": 80, "down_below": 64, "level_since": T0,
                    "critical": {"active": False, "since": None, "alerts": 0}, "override": None,
                    "watchdog": 60, "hysteresis": 6, "critical_temp": 90, "config_mtime": None,
                    "config_schema": 2, "rpm_by_level": {}, "top_cycles_10min": 0,
                    "external_write_detected": False}
        for key, value in expected.items():
            self.assertEqual(st[key], value, key)
        self.assertEqual(st["dwell_remaining_s"], 42)          # 45 s dwell since the seat at t=0
        json.dumps(st)
        top = Rig()
        top.step(85.0)
        st = top.ctrl.state()
        self.assertEqual((st["up_at"], st["down_below"]), (None, 72))
        base = Rig()
        base.step(40.0)
        st = base.ctrl.state()
        self.assertEqual((st["up_at"], st["down_below"], st["dwell_remaining_s"]), (50, None, 0))

    def test_history_shape_and_bounds(self):
        rig = Rig()
        rig.run(60.0, 1900)
        h = rig.ctrl.history()
        self.assertEqual((h["schema"], h["sample_interval"], len(h["samples"])), (1, 1, 1800))
        ts, raw, fast, slow, level, rpm, ac = h["samples"][-1]
        self.assertEqual((ts, raw, level, rpm, ac), (T0 + 1899, 60.0, "6", 3600, True))
        self.assertEqual((fast, slow), (60.0, 60.0))
        for i in range(300):
            rig.ctrl.event("config", f"n{i}")
        h = rig.ctrl.history()
        self.assertEqual(len(h["events"]), 200)
        self.assertEqual(h["events"][-1][1:], ["config", "n299"])
        json.dumps(h)

    def test_top_cycles_10min(self):
        rig = Rig({"dwell_down_s": 0, "step_down_spacing_s": 0,
                   "smoothing_up_s": 2, "smoothing_down_s": 5})
        rig.run(75.0, 5)
        for _ in range(3):
            rig.run(88.0, 10)
            rig.run(60.0, 20)
        self.assertEqual(rig.ctrl.state()["top_cycles_10min"], 3)
        rig.run(60.0, 600)
        self.assertEqual(rig.ctrl.state()["top_cycles_10min"], 0)

    def test_rpm_learning(self):
        rig = Rig()
        rig.run(75.0, 20)
        self.assertEqual(rig.ctrl.rpm_by_level, {})              # not yet 20 s at "7"
        rig.run(75.0, 5)
        self.assertEqual(rig.ctrl.rpm_by_level, {"7": 4000})
        self.assertEqual(rig.hal.saved_rpm, {"7": 4000})
        with mock.patch.dict(D.SIM_RPM, {"7": 4040}):           # < 50 RPM: not persisted
            rig.run(75.0, 60)
        self.assertEqual(rig.ctrl.rpm_by_level["7"], 4040)
        self.assertEqual(rig.hal.saved_rpm, {"7": 4000})
        with mock.patch.dict(D.SIM_RPM, {"7": 4100}):
            rig.run(75.0, 60)
        # Persisted when the median first moved >= 50 RPM from the saved 4000:
        # at the 25/25 split of the 50-sample window it reads (4040+4100)/2 =
        # 4070; the final 4100 is within 50 of that and stays in memory only.
        self.assertEqual(rig.ctrl.rpm_by_level["7"], 4100)
        self.assertEqual(rig.hal.saved_rpm, {"7": 4070})

    def test_atomic_json_is_0644_under_umask_077(self):
        target = self.path("state.json")
        old = os.umask(0o077)
        try:
            D.write_json_atomic(target, {"a": 1})
            D.write_json_atomic(target, {"a": 2}, durable=True)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o644)
        with open(target, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a": 2})
        self.assertEqual(os.listdir(self.tmp), ["state.json"])   # no temp file left behind


# ── §10 log format ───────────────────────────────────────────────────────────

class LogFormat(TempDirCase):
    STAMP = r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] "

    def test_startup_and_change_lines(self):
        rig = Rig()
        rig.run(75.0, 5)
        rig.run(86.0, 30)
        self.assertRegex(rig.log.lines[0], self.STAMP + r"Daemon v2\.0\.0 started \(pid 4242, "
                         r"sample 1 s, watchdog 60 s, critical 90, "
                         r"ac 0:auto 50:4 60:6 70:7 80/72:disengaged \| "
                         r"battery 0:auto 60:3 70:5 80:7 87/78:disengaged\)$")
        up = rig.lines("Fan 7 -> disengaged")
        self.assertEqual(len(up), 1)
        self.assertRegex(up[0], self.STAMP + r"Fan 7 -> disengaged \(raw 86\.0 fast \d+\.\d "
                         r"slow \d+\.\d \| step 4/5 ac \| up>=80 down<72\) \[curve\]$")
        self.assertTrue(all(re_line.startswith("[") for re_line in rig.log.lines))

    def test_warn_rate_limit_and_count(self):
        clock = D.FakeClock(0.0, T0)
        log = D.Log(self.path("x.log"), clock)
        for _ in range(5):
            log.warn("WARN: flaky")
        clock.advance(59)
        log.warn("WARN: flaky")
        clock.advance(2)
        log.warn("WARN: flaky")
        log.error("ERROR: other")
        with open(self.path("x.log"), encoding="utf-8") as f:
            lines = [line.rstrip("\n")[22:] for line in f]
        self.assertEqual(lines, ["WARN: flaky", "WARN: flaky (x6)", "ERROR: other"])

    def test_log_file_created_0644_under_umask_077(self):
        old = os.umask(0o077)
        try:
            D.Log(self.path("new.log")).info("hello")
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(self.path("new.log")).st_mode), 0o644)


# ── hardware helpers (hwmon by name, /proc parsing, sd_notify, alerts) ───────

FAN_PROC_TEXT = ("status:\t\t{status}\nspeed:\t\t{speed}\nlevel:\t\t{level}\n"
                 "commands:\tlevel <level> (<level> is 0-7, auto, disengaged, full-speed)\n"
                 "commands:\tenable, disable\n"
                 "commands:\twatchdog <timeout> (<timeout> is 0 (off), 1-120 (seconds))\n")


class Hardware(TempDirCase):

    def test_parse_fan_proc(self):
        info = D.parse_fan_proc(FAN_PROC_TEXT.format(status="disabled", speed=0, level="0"))
        self.assertEqual(info, {"status": "disabled", "speed": 0, "level": "0", "control_available": True})
        no_ctl = "status:\t\tenabled\nspeed:\t\t2650\nlevel:\t\tauto\n"
        self.assertEqual(D.parse_fan_proc(no_ctl),
                         {"status": "enabled", "speed": 2650, "level": "auto", "control_available": False})
        only_enable = no_ctl + "commands:\tenable, disable\n"
        self.assertFalse(D.parse_fan_proc(only_enable)["control_available"])
        self.assertEqual(D.parse_fan_proc(None),
                         {"status": None, "speed": None, "level": None, "control_available": False})

    def test_hwmon_found_by_name_and_renumbering(self):
        root = self.path("hwmon")
        self.write("hwmon/hwmon0/name", "amdgpu\n")
        self.write("hwmon/hwmon0/temp1_input", "64500\n")
        self.write("hwmon/hwmon1/name", "k10temp\n")
        self.write("hwmon/hwmon1/temp1_input", "65432\n")
        self.write("hwmon/hwmon2/name", "nvme\n")
        with mock.patch.object(D, "HWMON_ROOT", root):
            hal = D.LiveHAL(D.Log(None))
            self.assertEqual(hal.read_temps(), (65.432, 64.5))
            os.rename(self.path("hwmon/hwmon0"), self.path("hwmon/tmp"))
            os.rename(self.path("hwmon/hwmon1"), self.path("hwmon/hwmon0"))
            os.rename(self.path("hwmon/tmp"), self.path("hwmon/hwmon1"))
            self.assertEqual(hal.read_temps(), (65.432, 64.5))
            for text in ("0", "150000", "abc", ""):
                self.write("hwmon/hwmon0/temp1_input", text)
                self.assertEqual(hal.read_temps()[0], None, text)
            os.unlink(self.path("hwmon/hwmon1/temp1_input"))
            self.assertEqual(hal.read_temps(), (None, None))

    def test_live_fan_write_reports_failure(self):
        log = D.Log(None, collect=True)
        hal = D.LiveHAL(log)
        target = self.write("fan", "")
        with mock.patch.object(D, "FAN_PROC", target):
            self.assertNotEqual(D.FAN_PROC, "/proc/acpi/ibm/fan")
            self.assertTrue(hal.fan_write("level 3"))
            with open(target, encoding="ascii") as f:
                self.assertEqual(f.read(), "level 3")
        with mock.patch.object(D, "FAN_PROC", self.path("nodir", "fan")):
            self.assertFalse(hal.fan_write("level 3"))
        self.assertTrue(any("ERROR: writing 'level 3'" in line for line in log.lines))

    def test_sd_notify(self):
        addr = self.path("notify.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.bind(addr)
            s.settimeout(2)
            with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": addr}):
                D.sd_notify("READY=1")
            self.assertEqual(s.recv(64), b"READY=1")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTIFY_SOCKET", None)
            D.sd_notify("WATCHDOG=1")                           # no-op, no error

    def test_gui_identity(self):
        gui = D.GuiIdentity({})
        self.assertEqual((gui.uid, gui.gid, gui.runtime_dir), (1000, 1000, "/run/user/1000"))
        try:
            import pwd
            expected_home = "/home/" + pwd.getpwuid(1000).pw_name
        except KeyError:
            expected_home = "/"
        self.assertEqual(gui.home, expected_home)
        gui = D.GuiIdentity({"FANCTL_GUI_UID": "1001", "FANCTL_GUI_GID": "x",
                             "FANCTL_GUI_HOME": "relative", "FANCTL_GUI_RUNTIME_DIR": "/run/user/77"})
        self.assertEqual((gui.uid, gui.gid, gui.runtime_dir), (1001, 1000, "/run/user/77"))

class FakeProc:
    """Stands in for a player process; nothing is ever spawned by these tests."""
    _pids = itertools.count(4_000_000)

    def __init__(self):
        self.rc = None
        self.pid = next(self._pids)

    def poll(self):
        return self.rc


class AlertPlayerChain(TempDirCase):
    """§8 player chain with subprocess.Popen and the kill path replaced by fakes."""

    def setUp(self):
        super().setUp()
        self.clock = D.FakeClock(0.0, T0)
        self.log = D.Log(None, self.clock, collect=True)
        self.gui = D.GuiIdentity({"FANCTL_GUI_UID": str(os.getuid()), "FANCTL_GUI_GID": str(os.getgid()),
                                  "FANCTL_GUI_HOME": self.tmp, "FANCTL_GUI_RUNTIME_DIR": "/run/user/4321"})
        self.player = D.AlertPlayer(self.gui, self.log, self.clock)
        self.spawned = []
        popen = mock.patch.object(D.subprocess, "Popen", side_effect=self.fake_popen)
        self.popen = popen.start()
        self.addCleanup(popen.stop)
        kill = mock.patch.object(D.AlertPlayer, "_kill")
        self.kill = kill.start()
        self.addCleanup(kill.stop)
        self.sound = self.write("alert.mp3", "not really audio", 0o644)

    def fake_popen(self, argv, **kwargs):
        proc = FakeProc()
        self.spawned.append((argv, kwargs, proc))
        return proc

    def argvs(self):
        return [argv for argv, _kw, _p in self.spawned]

    def test_spawn_as_the_gui_user_without_sudo(self):
        self.player.play(self.sound)
        argv, kw, _proc = self.spawned[0]
        self.assertEqual(argv, ["paplay", self.sound])
        self.assertEqual((kw["user"], kw["group"], kw["extra_groups"], kw["cwd"]),
                         (os.getuid(), os.getgid(), [], "/"))
        self.assertEqual(kw["env"], {"XDG_RUNTIME_DIR": "/run/user/4321",
                                     "PULSE_SERVER": "unix:/run/user/4321/pulse/native",
                                     "PATH": "/usr/bin:/bin", "HOME": self.tmp})
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(kw[stream], subprocess.DEVNULL)
        self.assertTrue(kw["start_new_session"])

    def test_fallback_chain(self):
        self.player.play(self.sound)
        self.spawned[0][2].rc = 1
        self.clock.advance(1)
        self.player.poll()
        self.assertEqual(self.argvs()[1],
                         ["gst-play-1.0", "--no-interactive", "--volume=1.0", self.sound])
        self.spawned[1][2].rc = 1
        self.clock.advance(1)
        self.player.poll()
        self.assertEqual(self.argvs()[2], ["paplay", D.FALLBACK_WAV])
        self.spawned[2][2].rc = 0
        self.player.poll()
        self.assertEqual((len(self.spawned), self.player.children), (3, []))

    def test_late_paplay_failure_is_not_retried(self):
        self.player.play(self.sound)
        self.clock.advance(3)
        self.spawned[0][2].rc = 1
        self.player.poll()
        self.assertEqual(len(self.spawned), 1)
        self.assertTrue(any("alert player paplay failed" in line for line in self.log.lines))

    def test_unreadable_sound_uses_the_fallback_wav(self):
        os.chmod(self.sound, 0)
        self.player.play(self.sound)
        self.assertEqual(self.argvs(), [["paplay", D.FALLBACK_WAV]])
        self.player.play(self.path("missing.mp3"))
        self.assertEqual(self.argvs()[1], ["paplay", D.FALLBACK_WAV])

    def test_never_as_root(self):
        player = D.AlertPlayer(D.GuiIdentity({"FANCTL_GUI_UID": "0"}), self.log, self.clock)
        player.play(self.sound)
        self.assertEqual(self.spawned, [])
        self.assertTrue(any("FANCTL_GUI_UID is 0" in line for line in self.log.lines))

    def test_stuck_player_is_killed_after_30s(self):
        self.player.play(self.sound)
        self.clock.advance(29)
        self.player.poll()
        self.kill.assert_not_called()
        self.clock.advance(2)
        self.player.poll()
        self.kill.assert_called_once_with(self.spawned[0][2])
        self.assertEqual(self.player.children, [])

    def test_missing_player_binary_is_logged(self):
        self.popen.side_effect = FileNotFoundError(2, "No such file", "paplay")
        self.player.play(self.sound)
        self.assertTrue(any("cannot start alert player paplay" in line for line in self.log.lines))


class ReadableBy(TempDirCase):

    def test_readable_by(self):
        me, grp = os.getuid(), os.getgid()
        private = self.write("private.wav", "x", 0o600)
        self.assertTrue(D.readable_by(private, me, grp))
        self.assertFalse(D.readable_by(private, me + 4321, grp + 4321))
        self.assertFalse(D.readable_by(self.tmp, me, grp))                # not a regular file
        self.assertFalse(D.readable_by(self.path("missing"), me, grp))
        os.symlink(private, self.path("link.wav"))
        self.assertTrue(D.readable_by(self.path("link.wav"), me, grp))


# ── §7 control socket, end to end in-process ─────────────────────────────────

class SocketInProcess(TempDirCase):
    """
    The real ControlServer and LiveDaemon loop (in a thread, real clock) on a
    socket in a temp dir; the fan is the simulator's fake.
    """

    def setUp(self):
        super().setUp()
        self.clock = D.SystemClock()
        self.hal = D.SimHAL(self.clock)
        self.hal.cpu, self.hal.igpu = 60.0, 58.0
        self.log = D.Log(self.path("daemon.log"))
        self.gui = D.GuiIdentity({"FANCTL_GUI_UID": str(os.getuid()),
                                  "FANCTL_GUI_GID": str(os.getgid()), "FANCTL_GUI_HOME": self.tmp})
        self.ctrl = D.Controller(D.sanitize({})[0], self.hal, self.clock, self.log,
                                 initial_level="auto", pid=os.getpid())
        self.sock_path = self.path("control.sock")
        self.server = D.ControlServer(self.sock_path, self.gui, self.ctrl, self.log)
        self.server.open()
        self.daemon = D.LiveDaemon(self.ctrl, self.log, self.tmp, self.server, None, self.clock)
        self.ctrl.start()
        self.ctrl.tick()
        self.daemon.publish_state()
        self.result = []
        self.thread = threading.Thread(target=lambda: self.result.append(self.daemon.run()), daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.daemon.request_stop()
        self.thread.join(5)
        self.server.close()
        self.daemon.close()

    def ask(self, data, half_close=False, path=None):
        """
        Send `data`, return everything received until the daemon closes.
        Linux reports ECONNRESET instead of EOF when the daemon closes with
        some of our bytes unread (oversize request, rejected peer); that is
        "closed" here as well.
        """
        buf = b""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(path or self.sock_path)
            s.sendall(data)
            if half_close:
                s.shutdown(socket.SHUT_WR)
            try:
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            except ConnectionResetError:
                pass
        return buf

    def call(self, obj):
        raw = self.ask((json.dumps(obj) + "\n").encode("utf-8"))
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)                        # exactly one JSON line
        return json.loads(raw)

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail("condition not reached in time")

    def read_json(self, name):
        try:
            with open(self.path(name), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def read_state(self):
        return self.read_json("state.json")

    def test_protocol(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.sock_path).st_mode), 0o660)
        reply = self.call({"cmd": "status"})
        self.assertEqual((reply["ok"], set(reply["state"])), (True, STATE_KEYS))

        reply = self.call({"cmd": "hold", "level": "3", "seconds": 600})
        self.assertTrue(reply["ok"])
        self.assertEqual(self.hal.level, "3")                 # the write preceded the reply
        self.assertEqual((reply["state"]["level"], reply["state"]["reason"]), ("3", "override"))
        st = self.wait_for(lambda: (s := self.read_state()) and s["override"] and s)
        self.assertEqual(st["override"]["level"], "3")
        self.assertEqual(stat.S_IMODE(os.stat(self.path("state.json")).st_mode), 0o644)
        # history.json is also rewritten every 10 s, so wait for the copy
        # published after the hold rather than any copy.
        self.wait_for(lambda: (h := self.read_json("history.json"))
                      and "override_start" in [e[1] for e in h["events"]])

        self.assertEqual(self.call({"cmd": "hold", "level": "0"})["error"], "too_hot_for_fan_off")
        reply = self.call({"cmd": "resume"})
        self.assertEqual((reply["ok"], reply["state"]["override"], self.hal.level), (True, None, "6"))

        self.assertEqual(self.call({"cmd": "test_alert"}), {"ok": True})
        self.assertEqual(self.call({"cmd": "test_alert"})["error"], "rate_limited")
        self.assertEqual(len(self.hal.alerts), 1)              # recorded by the fake, never played

        for payload in (b"garbage\n", b'{"cmd":"dance"}\n', b"[]\n"):
            self.assertEqual(json.loads(self.ask(payload))["error"], "bad_request")
        # Oversize: the reply is sent, but the kernel may discard it as a
        # reset because the rest of the request was never read.  Either way
        # nothing is executed and the daemon keeps serving.
        raw = self.ask(b'{"cmd":"hold","level":"7","seconds":60,"pad":"' + b"x" * 5000 + b'"}\n')
        if raw:
            self.assertEqual(json.loads(raw)["error"], "bad_request")
        self.assertIsNone(self.ctrl.override)
        self.assertTrue(self.call({"cmd": "status"})["ok"])
        # No trailing newline but a half-closed write side: still one request.
        reply = json.loads(self.ask(b'{"cmd":"status"}', half_close=True))
        self.assertTrue(reply["ok"])

        self.hal.control_available = False
        reply = self.call({"cmd": "hold", "level": "5", "seconds": 60})
        self.assertEqual(reply["error"], "fan_control_unavailable")
        self.assertTrue(self.thread.is_alive())

    def test_silent_client_does_not_stall_the_loop(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as idle:
            idle.connect(self.sock_path)                      # sends nothing
            t = time.monotonic()
            self.assertTrue(self.call({"cmd": "status"})["ok"])
            self.assertLess(time.monotonic() - t, 3.0)
            idle.settimeout(3)
            self.assertEqual(idle.recv(100), b"")             # closed after the 0.5 s timeout

    def test_handler_bug_is_contained(self):
        with mock.patch.object(self.ctrl, "handle_request", side_effect=RuntimeError("boom")):
            reply = self.call({"cmd": "status"})
        self.assertEqual((reply["ok"], reply["error"]), (False, "internal_error"))
        self.assertTrue(self.call({"cmd": "status"})["ok"])
        self.assertTrue(self.thread.is_alive())

    def test_foreign_peer_is_closed_silently(self):
        gui = D.GuiIdentity({"FANCTL_GUI_UID": str(os.getuid() + 4321),
                             "FANCTL_GUI_GID": str(os.getgid())})
        other = D.ControlServer(self.path("other.sock"), gui, self.ctrl, self.log)
        other.open()
        self.addCleanup(other.close)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            # An AF_UNIX connect() completes against the listen backlog, so
            # the connection is already pending for handle() to accept.
            s.connect(self.path("other.sock"))
            s.sendall(b'{"cmd":"hold","level":"7"}\n')
            self.assertFalse(other.handle())
            try:
                data = s.recv(100)
            except ConnectionResetError:        # closed with our request unread
                data = b""
            self.assertEqual(data, b"")                         # no reply at all
        self.assertIsNone(self.ctrl.override)

    def test_stop_request_ends_the_loop(self):
        self.daemon.request_stop()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.result, [0])

# ── the real binary: run loop, socket, SIGTERM, --self-test (fake paths) ─────

def scrubbed_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in DAEMON_ENV_KEYS}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


class FakeEC(threading.Thread):
    """
    Plays thinkpad_acpi for the real binary on a plain file: each command the
    daemon writes into it is applied and the file is re-rendered in the
    /proc/acpi/ibm/fan format, `commands:` lines included (fan_control=1).
    Polls every 2 ms; the daemon only writes on a change or a refresh, so it
    never reads a half-emulated file in the cases these tests look at.
    """

    SPEED = {"auto": 2600, "0": 0, "disengaged": 6400, "full-speed": 6400}

    def __init__(self, path, level="auto"):
        super().__init__(daemon=True)
        self.path = path
        self.level = level
        self.shown = None            # level of the last completed /proc-format render
        self.watchdog = None
        self.commands = []
        self._halt = threading.Event()
        self.render()

    def render(self):
        level = "disengaged" if self.level == "full-speed" else self.level
        with open(self.path, "w", encoding="ascii") as f:
            f.write(FAN_PROC_TEXT.format(status="disabled" if level == "0" else "enabled",
                                         speed=self.SPEED.get(level, 3000), level=level))
        # Published only now: a test waiting on `shown` knows the daemon's
        # next read of the file sees /proc format again, not a raw command.
        self.shown = self.level

    def run(self):
        while not self._halt.wait(0.002):
            try:
                with open(self.path, encoding="ascii", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            if not text or text.startswith("status:"):
                continue
            for line in text.splitlines():
                cmd = line.strip()
                if cmd.startswith("level "):
                    self.level = cmd[6:]
                    self.commands.append(cmd)
                elif cmd.startswith("watchdog "):
                    self.watchdog = int(cmd[9:])
                    self.commands.append(cmd)
            self.render()

    def halt(self):
        self._halt.set()
        self.join(2)


class RealBinary(TempDirCase):

    def setUp(self):
        super().setUp()
        self.write("hwmon/hwmon3/name", "k10temp\n")
        self.write("hwmon/hwmon3/temp1_input", "61000\n")
        self.write("hwmon/hwmon7/name", "amdgpu\n")
        self.write("hwmon/hwmon7/temp1_input", "59000\n")
        self.write("ac_online", "1\n")
        for d in ("run", "state", "rt"):
            os.makedirs(self.path(d))
        self.fan = self.path("fan")
        self.env = scrubbed_env(
            FANCTL_CONFIG_FILE=self.path("config.json"), FANCTL_LOG_FILE=self.path("daemon.log"),
            FANCTL_FAN_PROC=self.fan, FANCTL_HWMON_ROOT=self.path("hwmon"),
            FANCTL_AC_ONLINE=self.path("ac_online"), FANCTL_RUNTIME_DIR=self.path("run"),
            FANCTL_STATE_DIR=self.path("state"), FANCTL_GUI_UID=str(os.getuid()),
            FANCTL_GUI_GID=str(os.getgid()), FANCTL_GUI_HOME=self.tmp,
            FANCTL_GUI_RUNTIME_DIR=self.path("no-session"))

    def log_text(self):
        with open(self.path("daemon.log"), encoding="utf-8", errors="replace") as f:
            return f.read()

    def wait_for(self, predicate, timeout=15.0, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail(f"{what} not reached within {timeout} s; log:\n{self.log_text()}")

    def call(self, obj):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(self.path("run", "control.sock"))
            s.sendall((json.dumps(obj) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf)

    def read_state(self):
        try:
            with open(self.path("run", "state.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def test_run_hold_resume_and_sigterm_hand_back(self):
        ec = FakeEC(self.fan)
        ec.start()
        self.addCleanup(ec.halt)
        with open(self.path("stderr.txt"), "w") as err:
            proc = subprocess.Popen([sys.executable, str(DAEMON)], env=self.env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)

        def reap():
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)
        self.addCleanup(reap)

        st = self.wait_for(lambda: (s := self.read_state()) and s["level"] == "6" and s,
                           what="state.json with the curve level")
        self.assertEqual((st["version"], st["pid"], st["sample_interval"]), ("2.0.0", proc.pid, 1))
        self.assertLess(time.time() - st["ts"], 3 * st["sample_interval"] + 2)   # §5 liveness
        self.assertEqual(set(st), STATE_KEYS)
        self.assertEqual((st["temp_raw"], st["cpu"], st["igpu"], st["watchdog"]), (61.0, 61.0, 59.0, 60))
        self.wait_for(lambda: ec.shown == "6", what="fan at the curve level")
        self.assertEqual(stat.S_IMODE(os.stat(self.path("run", "control.sock")).st_mode), 0o660)
        self.assertEqual(stat.S_IMODE(os.stat(self.path("run", "state.json")).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(self.path("daemon.log")).st_mode), 0o644)

        time.sleep(0.2)
        reply = self.call({"cmd": "hold", "level": "3", "seconds": 300})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual((reply["state"]["level"], reply["state"]["reason"]), ("3", "override"))
        self.wait_for(lambda: ec.shown == "3", what="fan at the held level")
        self.assertEqual(self.call({"cmd": "hold", "level": "0"})["error"], "too_hot_for_fan_off")
        reply = self.call({"cmd": "resume"})
        self.assertEqual((reply["ok"], reply["state"]["override"]), (True, None))
        self.wait_for(lambda: ec.shown == "6", what="fan back on the curve")

        ec.halt()                        # from here the file keeps the daemon's raw writes
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(10), 0)
        with open(self.fan, encoding="ascii") as f:
            self.assertEqual(f.read(), "level auto")                 # the last write: shutdown()
        log = self.log_text()
        for text in ("Daemon v2.0.0 started", f"Override 3 for 300s (uid {os.getuid()})",
                     f"Override 3 ended (resumed by uid {os.getuid()})",
                     "Daemon stopping — returning fan to firmware control"):
            self.assertIn(text, log)
        self.assertFalse(os.path.exists(self.path("run", "state.json")))
        self.assertFalse(os.path.exists(self.path("run", "control.sock")))

    def test_self_test(self):
        self.write("fan", FAN_PROC_TEXT.format(status="enabled", speed=3100, level="4"))
        env = dict(self.env, RUNTIME_DIRECTORY=self.path("rt"))
        proc = subprocess.run([sys.executable, str(DAEMON), "--self-test"], env=env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr + self.log_text())
        self.assertIn("Self-test OK (level 4 written back", proc.stdout)
        with open(self.fan, encoding="ascii") as f:
            self.assertEqual(f.read(), "level 4")                    # read-back written back
        with open(self.path("rt", "state.json"), encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(set(st), STATE_KEYS)
        # A plain file no longer looks like /proc after the write-back, so
        # only what does not depend on re-reading it is checked here.
        self.assertEqual((st["level"], st["curve_level"], st["temp_raw"]), ("4", "6", 61.0))
        self.assertNotIn("Fan ", self.log_text())                   # commanded nothing else
        # RUNTIME_DIRECTORY (systemd) wins over the FANCTL_RUNTIME_DIR test override.
        self.assertEqual(os.listdir(self.path("run")), [])

    def test_self_test_fails_loudly_when_the_fan_is_read_only(self):
        self.write("fan", FAN_PROC_TEXT.format(status="enabled", speed=3100, level="4"), 0o444)
        env = dict(self.env, RUNTIME_DIRECTORY=self.path("rt"))
        proc = subprocess.run([sys.executable, str(DAEMON), "--self-test"], env=env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FATAL: self-test write 'level 4'", self.log_text())
        self.assertEqual(os.listdir(self.path("rt")), [])

    def test_refuses_the_real_paths_without_root(self):
        env = scrubbed_env(FANCTL_LOG_FILE=self.path("daemon.log"))
        proc = subprocess.run([sys.executable, str(DAEMON)], env=env, capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("must run as root", proc.stderr)


class RuntimeDirPrecedence(unittest.TestCase):

    def test_systemd_directories_win(self):
        self.assertEqual(load_daemon({}).RUN_DIR, "/run/thinkpad-fan-control")
        self.assertEqual(load_daemon({"FANCTL_RUNTIME_DIR": "/x/test-run"}).RUN_DIR, "/x/test-run")
        m = load_daemon({"FANCTL_RUNTIME_DIR": "/x/test-run", "FANCTL_STATE_DIR": "/x/test-state",
                         "RUNTIME_DIRECTORY": "/run/thinkpad-fan-control",
                         "STATE_DIRECTORY": "/var/lib/thinkpad-fan-control"})
        self.assertEqual((m.RUN_DIR, m.STATE_DIR),
                         ("/run/thinkpad-fan-control", "/var/lib/thinkpad-fan-control"))

    def test_overrides_ignored_as_root(self):
        env = {"FANCTL_FAN_PROC": "/x/fan", "FANCTL_CONFIG_FILE": "/x/c.json",
               "FANCTL_RUNTIME_DIR": "/x/run", "FANCTL_LOG_FILE": "/x/log"}
        with mock.patch("os.geteuid", return_value=0):
            m = load_daemon(env)
        self.assertEqual((m.FAN_PROC, m.CONFIG_FILE, m.RUN_DIR, m.LOG_FILE),
                         ("/proc/acpi/ibm/fan", "/etc/thinkpad-fan-control/config.json",
                          "/run/thinkpad-fan-control", "/var/log/thinkpad-fan-control.log"))


# ── §13 command line: --version, --gen-trace, --simulate ─────────────────────

SUMMARY_KEYS = {"changes_per_hour", "transitions", "median_dwell_s", "min_dwell_s",
                "min_gap_opposite_s", "seconds_at_or_above_critical", "time_share_per_level", "alerts"}


class CommandLine(TempDirCase):

    def cli(self, *args, ok=True):
        proc = subprocess.run([sys.executable, str(DAEMON), *args], env=scrubbed_env(),
                              capture_output=True, text=True, timeout=180)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def test_version(self):
        self.assertEqual(self.cli("--version").stdout, "thinkpad-fan-controld 2.0.0\n")

    def test_gen_trace_is_deterministic_and_simulates(self):
        a = self.cli("--gen-trace", "load15", "--seconds", "600", "--seed", "3").stdout
        b = self.cli("--gen-trace", "load15", "--seconds", "600", "--seed", "3").stdout
        c = self.cli("--gen-trace", "load15", "--seconds", "600", "--seed", "4").stdout
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("t_s,cpu,igpu,ac,sensor_ok"))
        self.assertEqual(len(a.splitlines()), 601)
        trace = self.write("load15.csv", a)
        out = self.cli("--simulate", trace, "--json").stdout
        self.assertEqual(len(out.splitlines()), 1)
        summary = json.loads(out)
        self.assertLessEqual(SUMMARY_KEYS, set(summary))
        self.assertEqual((summary["mode"], summary["loop"]), ("v2", "closed"))
        what_if = json.loads(self.cli("--simulate", trace, "--json", "--set", "dwell_down_s=30").stdout)
        self.assertEqual(what_if["config"]["dwell_down_s"], 30)
        legacy = json.loads(self.cli("--simulate", trace, "--json", "--set", "legacy=1").stdout)
        self.assertEqual(legacy["mode"], "legacy")
        warned = self.cli("--simulate", trace, "--json", "--set", "hysteresis=99")
        self.assertIn("hysteresis", warned.stderr)
        text = self.cli("--simulate", trace).stdout
        self.assertIn("=== summary (v2, closed loop) ===", text)
        self.assertIn("Daemon v2.0.0 started", text)
        quiet = self.cli("--simulate", trace, "--quiet").stdout
        self.assertTrue(quiet.lstrip().startswith("=== summary"))

    def test_simulate_history_json_and_bad_input(self):
        samples = [[T0 + i, 60.0 + (i % 7), None, None, "6", 3600, True] for i in range(120)]
        path = self.write("history.json", json.dumps({"schema": 1, "sample_interval": 1,
                                                      "samples": samples, "events": []}))
        summary = json.loads(self.cli("--simulate", path, "--json").stdout)
        self.assertEqual((summary["loop"], summary["duration_s"]), ("open", 120.0))
        bad = self.cli("--simulate", self.path("missing.csv"), ok=False)
        self.assertEqual(bad.returncode, 2)
        bad = self.cli("--gen-trace", "nonsense", ok=False)
        self.assertNotEqual(bad.returncode, 0)

    def test_real_trace_v2_against_v1_rules(self):
        trace = str(REPO / "tests" / "sim" / "traces" / "real-2026-09-23-mini-eq-1hz.csv")
        v2 = json.loads(self.cli("--simulate", trace, "--json").stdout)
        v1 = json.loads(self.cli("--simulate", trace, "--json", "--set", "legacy=1").stdout)
        self.assertGreater(v1["transitions"], 0)
        self.assertLessEqual(v2["transitions"] * 3, v1["transitions"])


if __name__ == "__main__":
    unittest.main()
