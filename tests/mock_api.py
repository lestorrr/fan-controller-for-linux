#!/usr/bin/env python3
"""
Mock backend for the ThinkPad Fan Control v2 dashboard (docs/CONTRACT.md §14/§15).

Serves the repository's index.html (re-read on every request, so edits show on
reload) and fakes every HTTP endpoint with data that changes over time and has
the same shapes the real backend (fanlib.get_status / get_history /
settings_view / the POST handlers) returns. Nothing here touches hardware:
no /proc or /sys writes, no sudo, no systemctl, no sound, no daemon socket.

    python3 tests/mock_api.py --port 7150 --scenario curve
    python3 tests/mock_api.py --port 7151 --scenario unreachable --unreachable-after 30

Scenarios (--scenario):

    curve                    daemon on the AC curve; load cycles idle / medium / heavy (default)
    hold                     a 15 min hold at level 3 starts when serving starts
    hold_suspended           a hold at level 2 under heavy load, suspended above the hold ceiling
    critical                 heavy load: the daemon is in critical (forced Max) when serving starts
    daemon_down              daemon stopped, firmware auto (mode "firmware"); /api/history is empty
    unreachable              like curve, then every request hangs unanswered after --unreachable-after s
    smu_missing              ryzenadj --info fails: smu.ok=false, no values, power actions fail
    freeze_config            30 W with the VRM limits raised in hardware (freeze_config_warning)
    sensor_missing           no Wi-Fi hwmon; both die sensors invalid (daemon reason sensor_lost) for 2 min
    fan_control_unavailable  thinkpad_acpi without fan_control=1: no "commands: level" line, daemon down
  extra:
    manual_unprotected       daemon down, this backend's keep-alive holds level 3
    stale                    daemon hung: state.json stopped updating 40 s before serving
    battery                  on battery, battery curve in use

Options worth knowing: --seed-minutes (history length), --backend-age (minutes the
backend's own 2 s sampler has been running: older history samples have no
cpu/igpu/tdp, exactly like a freshly started app), --fail-saves (POST /api/config
fails), --log-nul (leave NUL padding in /api/log to exercise the page's guard),
--smu-tdp W (the STAPM limit the SMU reports at start, e.g. 38 to test a value
above the slider's range).

The controller is a compact port of the §4 rules (EMA filters, one UP step per
tick, DOWN gated by release temperature, dwell and spacing, critical entry and
exit to the top step, hold suspension on T_fast with T_slow + 30 s to resume,
sensor loss) driving the §13 first-order thermal model. It is a test double,
not the reference implementation. Python 3.12 standard library only.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import copy
import json
import math
import random
import re
import select
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

VERSION = "2.0.0"
REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "index.html"
CSP = "default-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:"
MAX_BODY = 65536

LEVELS = ["auto", "0", "1", "2", "3", "4", "5", "6", "7", "disengaged", "full-speed"]
RANK = {"auto": -1, "disengaged": 8, "full-speed": 8, **{str(i): i for i in range(8)}}

# Steady RPM per level on this T495 (level 7 ~4000, disengaged ~6400 measured).
STATIC_RPM = {"0": 0, "1": 2000, "2": 2400, "3": 2700, "4": 3000, "5": 3200,
              "6": 3500, "7": 4000, "disengaged": 6400, "full-speed": 6400}
# §13 thermal model: K/W per level (0-2 extrapolated), C = 50 J/K, ambient 36 °C, THM clamp 95.
R_BY_LEVEL = {"auto": 4.8, "0": 5.6, "1": 5.2, "2": 4.8, "3": 4.4, "4": 4.2, "5": 3.9,
              "6": 3.5, "7": 3.2, "disengaged": 2.25, "full-speed": 2.25}
C_JK = 50.0
T_AMB = 36.0
THM_CLAMP = 95.0
FAN_OFF_MAX_TEMP = 55

DEFAULT_CONFIG = {
    "schema": 2, "sample_interval": 1, "smoothing_up_s": 8, "smoothing_down_s": 30,
    "dwell_down_s": 45, "step_down_spacing_s": 10, "hysteresis": 6, "critical_temp": 90,
    "critical_exit_margin": 8, "critical_exit_hold_s": 60, "watchdog": 60,
    "override_max_seconds": 7200, "override_ceiling_temp": 82,
    "curve": [{"temp": 0, "level": "auto"}, {"temp": 50, "level": "4"}, {"temp": 60, "level": "6"},
              {"temp": 70, "level": "7"}, {"temp": 80, "level": "disengaged", "down": 72}],
    "battery_curve": [{"temp": 0, "level": "auto"}, {"temp": 60, "level": "3"}, {"temp": 70, "level": "5"},
                      {"temp": 80, "level": "7"}, {"temp": 87, "level": "disengaged", "down": 78}],
    "use_battery_curve": True, "alerts_enabled": True,
    "alert_sound": "/home/jhnlstrlclcn/Music/SYSTEM SOUND/90c.mp3", "alert_cooldown": 300,
}
DEFAULT_SETTINGS = {
    "schema": 1, "tdp_requested": None, "tdp_locked": False, "apply_tdp_at_startup": False,
    "last_clean_exit": True,
    "ui": {"hold_default_seconds": 900, "history_range_min": 15, "log_filter": "all", "view": "overview"},
}
_UI_WORD = re.compile(r"^[a-z0-9_-]{1,32}$")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _parse_int(v, digits=6):
    """fanlib._parse_int: a JSON int or a plain digit string; None for bool/float/garbage."""
    if _is_int(v):
        return v
    if isinstance(v, str) and re.fullmatch(r"0|[1-9]\d{0,%d}" % (digits - 1), v.strip()):
        return int(v.strip())
    return None


# ── §3 sanitize(raw, base): the daemon's rules, enough for `rejected` to be real ──

def _int_in(lo, hi):
    return lambda v: v if _is_int(v) and lo <= v <= hi else None


def _valid_curve(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
        return None
    out = []
    for i, step in enumerate(raw):
        if not isinstance(step, dict):
            return None
        temp = 0 if i == 0 else step.get("temp")
        level = step.get("level")
        if not _is_int(temp) or not 0 <= temp <= 105:
            return None
        if level not in RANK or (level == "auto" and i > 0):
            return None
        if out and temp <= out[-1]["temp"]:
            return None
        if out and RANK[level] < RANK[out[-1]["level"]]:
            return None
        if temp >= 60 and RANK[level] < 1:
            return None
        clean = {"temp": temp, "level": level}
        if step.get("down") is not None:
            down = step["down"]
            if not _is_int(down) or not temp - 20 <= down <= temp - 1:
                return None
            clean["down"] = down
        out.append(clean)
    return out


VALIDATORS = {
    "sample_interval": _int_in(1, 5), "smoothing_up_s": _int_in(2, 30), "smoothing_down_s": _int_in(5, 120),
    "dwell_down_s": _int_in(0, 300), "step_down_spacing_s": _int_in(0, 60), "hysteresis": _int_in(0, 20),
    "critical_temp": _int_in(70, 105), "critical_exit_margin": _int_in(3, 20),
    "critical_exit_hold_s": _int_in(10, 600),
    "watchdog": lambda v: v if _is_int(v) and (v == 0 or 15 <= v <= 120) else None,
    "override_max_seconds": _int_in(60, 14400), "alert_cooldown": _int_in(30, 3600),
    "use_battery_curve": lambda v: v if isinstance(v, bool) else None,
    "alerts_enabled": lambda v: v if isinstance(v, bool) else None,
    "alert_sound": lambda v: v if isinstance(v, str) and v.startswith("/") and len(v) <= 400 else None,
    "curve": _valid_curve, "battery_curve": _valid_curve,
}


def sanitize(raw, base):
    """Returns (config, rejected) with the daemon's precedence: raw, else base, else defaults."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    rejected = []
    for source, report in ((base, False), (raw, True)):
        if not isinstance(source, dict):
            continue
        for key, validator in VALIDATORS.items():
            if key in source:
                value = validator(source[key])
                if value is None:
                    if report:
                        rejected.append(key)
                else:
                    cfg[key] = copy.deepcopy(value)
    crit = cfg["critical_temp"]
    ceiling = crit - 8
    for source, report in ((base, False), (raw, True)):
        if isinstance(source, dict) and "override_ceiling_temp" in source:
            v = source["override_ceiling_temp"]
            if _is_int(v) and 60 <= v <= crit - 1:
                ceiling = v
            elif report:
                rejected.append("override_ceiling_temp")
    cfg["override_ceiling_temp"] = ceiling
    if cfg["watchdog"] > 0:
        cfg["watchdog"] = max(cfg["watchdog"], 3 * cfg["sample_interval"] + 15)
    cfg["schema"] = 2
    if isinstance(raw, dict):
        rejected.extend(k for k in raw if k not in DEFAULT_CONFIG and k not in ("schema", "poll_interval"))
    out = []
    for k in rejected:
        if k not in out:
            out.append(k)
    return cfg, out


def curve_summary(curve):
    return " ".join((f"{s['temp']}/{s['down']}" if "down" in s else str(s["temp"])) + ":" + s["level"] for s in curve)


# ── simulator ────────────────────────────────────────────────────────────────

class Sim:
    """Thermal model + §4 controller + fake backend state, stepped at 1 Hz in simulated epoch time."""

    DAEMON_DOWN = ("daemon_down", "fan_control_unavailable", "manual_unprotected")

    def __init__(self, scenario, seed_minutes=20, rng_seed=7, backend_age_min=5.0, unreachable_after=30.0,
                 fail_saves=False, log_nul=False, smu_tdp=None):
        self.lock = threading.RLock()
        self.scenario = scenario
        self.rng = random.Random(rng_seed)
        self.fail_saves = fail_saves
        self.log_nul = log_nul
        self.unreachable_after = unreachable_after
        now = time.time()
        self.serve_t0 = now                        # phase 0 = when serving starts
        self.t = now - seed_minutes * 60
        self.backend_since = now - backend_age_min * 60

        self.cfg = copy.deepcopy(DEFAULT_CONFIG)   # the daemon's running config
        self.file_cfg = copy.deepcopy(DEFAULT_CONFIG)
        self.file_mtime = round(now - 86400 * 3 + 0.123456, 6)
        self.cfg_mtime = self.file_mtime           # what the daemon loaded
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.autostart = True

        self.daemon_active = scenario not in self.DAEMON_DOWN
        self.daemon_enabled = True
        self.hung_at = None                        # "stale": state.json frozen at this time
        self.pid = 2304
        self.fan_control_available = scenario != "fan_control_unavailable"
        self.on_ac = scenario != "battery"
        self.wifi_present = scenario != "sensor_missing"

        # physics
        self.T = 47.0
        self.load = 6.0
        self.burst_left = 0
        self.burst_amp = 0.0
        self.rpm_f = 2400.0
        self.rpm = 2400
        self.cpu = self.igpu = self.T
        self.raw = self.T

        # controller
        self.t_fast = self.t_slow = self.T
        self.idx = 0
        self.last_up = self.last_change = self.t
        self.level = "auto"                        # commanded
        self.level_proc = "auto"                   # what /proc reports
        self.level_since = self.t
        self.reason = "curve"
        self.critical = False
        self.critical_since = None
        self.hot_count = 0
        self.episode_alerts = 0
        self.last_alert = -1e9
        self.override = None
        self.missing = 0
        self.sensor_lost = False
        self.top_entries = collections.deque()
        self.rpm_samples = collections.defaultdict(lambda: collections.deque(maxlen=50))
        self.rpm_by_level = {}
        self.external_until = None
        self.samples = collections.deque(maxlen=1800)
        self.events = collections.deque(maxlen=200)
        self.log_lines = []
        self.backend = collections.deque(maxlen=900)     # the backend's own 2 s sampler
        self.last_backend = -1e9

        # backend-side manual fallback (daemon down)
        self.fallback = None                       # {"level", "until", "critical"}
        self.last_test_alert = -1e9

        # SMU / TDP
        freeze = scenario == "freeze_config"
        self.smu_ok = scenario != "smu_missing"
        self.smu = {"tdp": 30 if freeze else 15, "power": 8.0, "power_slow": 8.0, "edc": 12.0,
                    "edc_limit": 60.0 if freeze else 45.0, "tdc": 9.0, "tdc_limit": 42.0 if freeze else 35.0,
                    "thm_limit": 95.0}
        if smu_tdp is not None:
            self.smu["tdp"] = int(smu_tdp)
        self.smu_updated = None
        self.vrm_desired = False
        if freeze:
            self.settings["tdp_requested"] = 30
        self.tdp_max, self.tdp_max_vrm = 35, 30

        # a realistic log: v1 leftovers, NUL padding from a past hard crash, a garbage line
        for stamp, msg in (("2026-09-22 13:36:04", "Fan → disengaged (control temp 80°C)"),
                           ("2026-09-22 13:37:13", "Fan → 7 (control temp 74°C)")):
            self.log_lines.append(f"[{stamp}] {msg}")
        self.log_lines.append("\x00" * 40)
        self.log_lines.append("Traceback (most recent call last): <stale v1 crash output>")
        # Markup probe: the page must render this as text (rows are built with textContent).
        self.log_lines.append('[2026-09-22 13:40:00] WARN: probe <img src=x onerror="window.__xss=1"> must stay text')
        if self.daemon_active or scenario == "stale":
            self._daemon_start_log()
        else:
            self.log("Daemon stopping — returning fan to firmware control")
        if scenario == "manual_unprotected":
            self.fallback = {"level": "3", "until": self.t + 7200, "critical": False}

        while self.t < now - 0.5:
            self.step()

    # ── helpers ──
    @property
    def phase(self):
        return self.t - self.serve_t0

    def log(self, msg):
        self.log_lines.append(time.strftime("[%Y-%m-%d %H:%M:%S] ", time.localtime(self.t)) + msg)
        if len(self.log_lines) > 6000:
            del self.log_lines[:1000]

    def event(self, kind, detail):
        self.events.append([round(self.t, 3), kind, detail])

    def _daemon_start_log(self):
        c = self.cfg
        self.log(f"Daemon v{VERSION} started (pid {self.pid}, sample {c['sample_interval']} s, "
                 f"watchdog {c['watchdog']} s, critical {c['critical_temp']}, "
                 f"ac {curve_summary(c['curve'])} | battery {curve_summary(c['battery_curve'])})")
        self.log(f"EC watchdog set to {c['watchdog']} s")
        self.event("daemon_start", f"v{VERSION}")

    def curve_name(self):
        return "battery" if (not self.on_ac and self.cfg["use_battery_curve"]) else "ac"

    def curve(self):
        return self.cfg["battery_curve" if self.curve_name() == "battery" else "curve"]

    def release(self, i):
        if i <= 0:
            return None
        s = self.curve()[i]
        return s["down"] if "down" in s else max(0, s["temp"] - self.cfg["hysteresis"])

    def reseat(self):
        c = self.curve()
        self.idx = max(i for i in range(len(c)) if c[i]["temp"] <= self.t_fast)
        self.last_up = self.last_change = self.t

    # ── scenario load (watts), relative to the serve time ──
    def load_target(self):
        ph, sc = self.phase, self.scenario
        if sc == "critical":
            return 30.0 if -420 <= ph < 900 else 7.0
        if sc == "hold":
            return 8.0
        if sc == "hold_suspended":
            return 19.0 if ph >= -900 else 9.0
        if sc == "freeze_config":
            return 16.0 if (ph % 300) < 200 else 9.0
        if sc in self.DAEMON_DOWN:
            return 7.5 + 3.0 * math.sin(ph / 97.0)
        c = ph % 480
        return 6.0 if c < 120 else 12.0 if c < 300 else 17.0 if c < 390 else 7.0

    def physical_level(self):
        lvl = self.level_proc                      # whatever /proc says is what the fan does
        if lvl == "auto":
            # crude EC behaviour for "level auto"
            t = self.T
            return "0" if t < 45 else "2" if t < 55 else "4" if t < 65 else "6" if t < 75 else "7"
        return lvl

    # ── one second ──
    def step(self):
        self.t += 1.0
        self.load += (self.load_target() - self.load) * 0.2
        phys = self.physical_level()
        R = R_BY_LEVEL.get(phys, 4.4)
        self.T += (T_AMB + self.load * R - self.T) / (R * C_JK)
        self.T = min(THM_CLAMP, self.T)
        # Tctl bursts (kept small here so the non-critical scenarios never trip critical by accident)
        if self.burst_left > 0:
            self.burst_left -= 1
        elif self.rng.random() < (1 / 25 if self.load > 10 else 1 / 60):
            self.burst_left = self.rng.randint(1, 3)
            self.burst_amp = self.rng.uniform(2, 6)
        burst = self.burst_amp if self.burst_left > 0 else 0.0
        self.cpu = self.T + burst + self.rng.uniform(-0.3, 0.3)
        self.igpu = self.T - self.rng.uniform(0.8, 3.0)
        target = STATIC_RPM.get(phys, 2400)
        self.rpm_f += (target - self.rpm_f) * 0.35
        self.rpm = 0 if target == 0 and self.rpm_f < 60 else int(round((self.rpm_f + self.rng.uniform(-25, 25)) / 10.0) * 10)
        lost = self.scenario == "sensor_missing" and -15 <= self.phase < 120
        raw = None if lost else max(self.cpu, self.igpu)
        self._smu_tick()
        if self.t - self.last_backend >= 2 and self.t >= self.backend_since:
            self.last_backend = self.t
            self.backend.append({"t": self.t, "tdp": self.smu["tdp"] if self.smu_ok else None,
                                 "power": self.smu["power"] if self.smu_ok else None,
                                 "cpu": None if lost else int(round(self.cpu)),
                                 "igpu": None if lost else int(round(self.igpu))})
        self.sensor_invalid = lost
        if self.scenario == "stale" and self.hung_at is None and self.phase >= -40:
            self.hung_at = self.t
            self.daemon_active = False
        if self.daemon_active:
            self._daemon_tick(raw)
        else:
            self._backend_fallback_tick(raw)
        self.scenario_hooks()

    def _smu_tick(self):
        if not self.smu_ok:
            return
        s = self.smu
        s["power"] = round(max(0.8, min(float(s["tdp"]) + 2.0, self.load * 0.95 + self.rng.uniform(-0.4, 0.4))), 1)
        s["power_slow"] = round(s["power_slow"] + (min(s["power"], float(s["tdp"])) - s["power_slow"]) * 0.08, 1)
        s["edc"] = round(min(s["edc_limit"], 4.0 + self.load * 1.9), 1)
        s["tdc"] = round(min(s["tdc_limit"], 3.0 + self.load * 1.2), 1)
        # the backend re-reads the SMU every 10 s (visible window)
        if self.smu_updated is None or self.t - self.smu_updated >= 10:
            self.smu_updated = self.t

    # ── the §4 controller, one tick ──
    def _daemon_tick(self, raw):
        cfg = self.cfg
        now = self.t
        c = self.curve()
        # 1. sensor loss
        if raw is None:
            self.missing += 1
            if self.missing >= 3:
                if not self.sensor_lost:
                    self.sensor_lost = True
                    target = "disengaged" if self.critical else "auto"
                    self.log(f"WARN: no valid temperature for {self.missing} samples; fan -> {target}")
                    self.event("sensor_lost", target)
                self.reason = "sensor_lost"
                self._write("disengaged" if self.critical else "auto", "sensor_lost")
            self.raw = None
            self._publish_sample()
            return
        if self.sensor_lost:
            self.sensor_lost = False
            self.log(f"Temperature sensors back (raw {raw:.1f}); re-seated")
            self.event("sensor_ok", f"raw {raw:.1f}")
            self.t_fast = self.t_slow = raw
            self.reseat()
        self.missing = 0
        self.raw = raw
        # 2. filters
        dt = cfg["sample_interval"]
        self.t_fast += (1 - math.exp(-dt / cfg["smoothing_up_s"])) * (raw - self.t_fast)
        self.t_slow += (1 - math.exp(-dt / cfg["smoothing_down_s"])) * (raw - self.t_slow)
        # 4. config reload (the daemon notices the file mtime on its next tick)
        if self.file_mtime != self.cfg_mtime:
            old = self.cfg
            self.cfg = copy.deepcopy(self.file_cfg)
            self.cfg_mtime = self.file_mtime
            if self.cfg != old:
                self.log(f"Config reloaded (sample {self.cfg['sample_interval']} s, watchdog {self.cfg['watchdog']} s)")
                self.event("config", "reloaded")
                self.reseat()
            cfg, c = self.cfg, self.curve()
        # 5. critical
        crit = cfg["critical_temp"]
        if not self.critical:
            self.hot_count = self.hot_count + 1 if raw >= crit else 0
            if self.hot_count >= 2 or raw >= crit + 3:
                self.critical, self.critical_since, self.episode_alerts = True, now, 0
                self.log(f"CRITICAL entered (raw {raw:.1f} fast {self.t_fast:.1f} slow {self.t_slow:.1f})")
                self.event("critical_on", f"raw {raw:.1f}")
                if cfg["alerts_enabled"] and now - self.last_alert >= cfg["alert_cooldown"]:
                    self._alert()
        else:
            if cfg["alerts_enabled"] and now - self.last_alert >= cfg["alert_cooldown"]:
                self._alert()
            if self.t_slow < crit - cfg["critical_exit_margin"] and now - self.critical_since >= cfg["critical_exit_hold_s"]:
                self.critical = False
                self.hot_count = 0
                self.log(f"CRITICAL cleared (slow {self.t_slow:.1f}) -> top step")
                self.event("critical_off", f"slow {self.t_slow:.1f}")
                self.idx = len(c) - 1                   # to the top step, not a re-seat (§4 step 5)
                self.last_up = self.last_change = now
        # 6. curve evaluation
        n = len(c)
        self.idx = min(self.idx, n - 1)
        if self.idx + 1 < n and self.t_fast >= c[self.idx + 1]["temp"]:
            self.idx += 1
            self.last_up = self.last_change = now
            if self.idx == n - 1:
                self.top_entries.append(now)
        elif (self.idx > 0 and self.t_slow < self.release(self.idx)
              and now - self.last_up >= cfg["dwell_down_s"] and now - self.last_change >= cfg["step_down_spacing_s"]):
            self.idx -= 1
            self.last_change = now
        while self.top_entries and now - self.top_entries[0] > 600:
            self.top_entries.popleft()
        curve_level = c[self.idx]["level"]
        # 7. target selection (suspension on T_fast, resume on T_slow + 30 s)
        ov = self.override
        if self.critical:
            target, reason = "disengaged", "critical"
        elif ov is not None:
            ceil = cfg["override_ceiling_temp"]
            if not ov["suspended"]:
                if ov["level"] != "auto" and self.t_fast >= ceil and RANK[curve_level] > RANK[ov["level"]]:
                    ov["suspended"], ov["suspended_at"] = True, now
                    self.log(f"Override {ov['level']} suspended (fast {self.t_fast:.1f} >= {ceil})")
            elif self.t_slow < ceil - cfg["hysteresis"] and now - ov["suspended_at"] >= 30:
                ov["suspended"] = False
                self.log(f"Override {ov['level']} resumed (slow {self.t_slow:.1f} < {ceil - cfg['hysteresis']})")
            target, reason = (curve_level, "override_suspended_hot") if ov["suspended"] else (ov["level"], "override")
        else:
            target, reason = curve_level, "curve"
        # 8. override expiry
        if ov is not None and ov["until"] <= now:
            self.log(f"Override {ov['level']} ended (expired)")
            self.event("override_end", f"{ov['level']} expired")
            self.override = None
            self.reseat()
            target, reason = ("disengaged", "critical") if self.critical else (self.curve()[self.idx]["level"], "curve")
        self.reason = reason
        self._write(target, reason)
        # 11. RPM learning
        if now - self.level_since >= 20:
            self.rpm_samples[self.level].append(self.rpm)
            med = int(statistics.median(self.rpm_samples[self.level]))
            if abs(self.rpm_by_level.get(self.level, -999) - med) >= 50 or self.level not in self.rpm_by_level:
                self.rpm_by_level[self.level] = med
        self._publish_sample()

    def _write(self, level, reason):
        if level == self.level:
            return
        c = self.curve()
        i = min(self.idx, len(c) - 1)
        th = f"up>={c[i]['temp']} down<{self.release(i)}" if i > 0 else (f"next>={c[1]['temp']}" if len(c) > 1 else "single step")
        raw = "--" if self.raw is None else f"{self.raw:.1f}"
        self.log(f"Fan {self.level} -> {level} (raw {raw} fast {self.t_fast:.1f} slow {self.t_slow:.1f} | "
                 f"step {i + 1}/{len(c)} {self.curve_name()} | {th}) [{reason}]")
        self.event("level", f"{self.level}->{level}")
        self.level = self.level_proc = level
        self.level_since = self.t

    def _alert(self):
        self.last_alert = self.t
        self.episode_alerts += 1
        since = time.strftime("%H:%M:%S", time.localtime(self.critical_since))
        self.log(f"ALERT: critical since {since} (raw {self.raw:.1f}, slow {self.t_slow:.1f})")
        self.event("alert", f"raw {self.raw:.1f}")

    def _publish_sample(self):
        self.samples.append([round(self.t, 3), None if self.raw is None else round(self.raw, 1),
                             round(self.t_fast, 1), round(self.t_slow, 1), self.level, self.rpm, self.on_ac])

    # ── daemon down: the backend's keep-alive (§14) or plain firmware ──
    def _backend_fallback_tick(self, raw):
        self.raw = raw
        fb = self.fallback
        if fb is None:
            if self.scenario != "stale":
                self.level_proc = "auto"
            return
        crit = self.cfg["critical_temp"]
        if raw is not None and raw >= crit and not fb["critical"]:
            fb["critical"] = True
            self.log_backend(f"keep-alive: {raw:.1f} °C >= critical {crit} °C, fan forced to disengaged")
        if not fb["critical"] and self.t >= fb["until"]:
            self.log_backend(f"keep-alive: hold {fb['level']} expired, fan back to firmware auto")
            self.fallback = None
            self.level_proc = "auto"
            return
        self.level_proc = "disengaged" if fb["critical"] else fb["level"]

    def log_backend(self, msg):
        # fanlib prints these on its own stdout, not into the daemon log; keep a copy for debugging
        self.backend_log = getattr(self, "backend_log", [])
        self.backend_log.append(msg)

    # ── scenario hooks that belong to "serving time", not to the seeded past ──
    def scenario_hooks(self):
        if self.scenario == "hold_suspended" and self.override is None and self.daemon_active and self.phase >= -600 \
                and not getattr(self, "_hs_done", False):
            self._hs_done = True
            self.cmd_hold("2", 3600)
        if self.scenario == "hold" and self.phase >= 0 and not getattr(self, "_hold_done", False):
            self._hold_done = True
            self.cmd_hold("3", 900)
        if self.scenario == "stale" and self.hung_at is not None and getattr(self, "frozen", None) is None:
            self.frozen = self.state_dict()        # the last state.json the hung daemon wrote

    # ── §7 socket commands, as the daemon would answer them ──
    def cmd_hold(self, level, secs):
        if not self.fan_control_available:
            return {"ok": False, "error": "fan_control_unavailable",
                    "message": "thinkpad_acpi offers no fan level control (no 'commands: level' line in "
                               "/proc/acpi/ibm/fan); load it with fan_control=1."}
        if level == "0" and (self.raw is None or self.raw >= FAN_OFF_MAX_TEMP):
            now_txt = "--" if self.raw is None else f"{self.raw:.1f}"
            return {"ok": False, "error": "too_hot_for_fan_off",
                    "message": f"Fan off is only allowed below {FAN_OFF_MAX_TEMP} °C (now {now_txt})."}
        limit = self.cfg["override_max_seconds"]
        secs = limit if secs == 0 else min(secs, limit)
        self.override = {"level": level, "until": self.t + secs, "set_at": self.t, "suspended": False, "suspended_at": None}
        self.log(f"Override {level} for {secs}s (uid 1000)")
        self.event("override_start", f"{level} {secs}s")
        if not self.critical:                      # the fan write happens before the reply
            self.reason = "override"
            self._write(level, "override")
        return {"ok": True, "state": self.state_dict()}

    def cmd_resume(self):
        if self.override is not None:
            self.log(f"Override {self.override['level']} ended (resumed by uid 1000)")
            self.event("override_end", f"{self.override['level']} resumed")
        self.override = None
        self.reseat()
        if not self.critical:
            self.reason = "curve"
            self._write(self.curve()[self.idx]["level"], "curve")
        return {"ok": True, "state": self.state_dict()}

    # ── §5 state.json ──
    def state_dict(self):
        c = self.curve()
        n = len(c)
        i = min(self.idx, n - 1)
        ov = None
        if self.override is not None:
            o = self.override
            ov = {"level": o["level"], "until": o["until"], "set_at": o["set_at"],
                  "remaining_s": max(0, int(math.ceil(o["until"] - self.t))), "suspended": o["suspended"]}
        dwell = 0 if i == 0 else int(math.ceil(max(self.cfg["dwell_down_s"] - (self.t - self.last_up),
                                                   self.cfg["step_down_spacing_s"] - (self.t - self.last_change), 0.0)))
        r1 = lambda v: None if v is None else round(v, 1)
        ok = self.raw is not None
        return {
            "schema": 1, "version": VERSION, "pid": self.pid, "ts": self.t, "mono": self.t - 1.7e9 + 5000.0,
            "sample_interval": self.cfg["sample_interval"], "sensor_ok": ok,
            "temp_raw": r1(self.raw), "temp_fast": r1(self.t_fast), "temp_slow": r1(self.t_slow),
            "cpu": r1(self.cpu) if ok else None, "igpu": r1(self.igpu) if ok else None,
            "level": self.level, "level_proc": self.level_proc, "rpm": self.rpm,
            "fan_control_available": self.fan_control_available, "fan_status": self.fan_status(),
            "reason": self.reason, "on_ac": self.on_ac, "curve": self.curve_name(),
            "step_index": i, "steps": n, "curve_level": c[i]["level"],
            "up_at": c[i + 1]["temp"] if i + 1 < n else None, "down_below": self.release(i),
            "level_since": self.level_since, "dwell_remaining_s": dwell,
            "critical": {"active": self.critical, "since": self.critical_since, "alerts": self.episode_alerts},
            "override": ov, "watchdog": self.cfg["watchdog"], "hysteresis": self.cfg["hysteresis"],
            "critical_temp": self.cfg["critical_temp"], "config_mtime": self.cfg_mtime, "config_schema": 2,
            "rpm_by_level": dict(self.rpm_by_level), "top_cycles_10min": len(self.top_entries),
            "external_write_detected": False,
        }

    def fan_status(self):
        # the raw EC word: "disabled" only means the fan register is 0 (level 0), never "no control"
        return "disabled" if self.level_proc == "0" else "enabled"

    # ── §15 /api/status (same keys and types as fanlib.get_status) ──
    def status(self):
        now = time.time()
        active = self.daemon_active
        st = self.state_dict() if active else None
        frozen = getattr(self, "frozen", None)
        age = now - st["ts"] if st else (now - frozen["ts"] if frozen else None)
        lost = self.sensor_invalid
        temps = {"cpu": None if lost else int(round(self.cpu)), "igpu": None if lost else int(round(self.igpu)),
                 "nvme": int(round(38 + self.load * 0.7)), "wifi": int(round(44 + self.load * 0.3)) if self.wifi_present else None}
        if st and st["temp_raw"] is not None:
            temp_c = round(float(st["temp_raw"]), 1)
        else:
            vals = [v for v in (temps["cpu"], temps["igpu"]) if v is not None]
            temp_c = max(vals) if vals else None
        level = self.level_proc
        if not active:
            mode = "firmware" if level in (None, "auto") else "manual_unprotected"
        else:
            mode = {"critical": "critical", "override": "hold", "override_suspended_hot": "hold_suspended",
                    "sensor_lost": "sensor_lost"}.get(self.reason, "curve")
        if self.smu_ok:
            smu = {"ok": True, "stale": False, **{k: self.smu[k] for k in ("tdp", "power", "power_slow", "edc", "edc_limit",
                                                                            "tdc", "tdc_limit", "thm_limit")},
                   "updated_at": self.smu_updated, "error": None}
        else:
            smu = {"ok": False, "stale": False, "tdp": None, "power": None, "power_slow": None, "edc": None,
                   "edc_limit": None, "tdc": None, "tdc_limit": None, "thm_limit": None, "updated_at": None,
                   "error": "ryzenadj --info failed: sudo: a password is required"}
        vrm_hw = smu["edc_limit"] is not None and smu["edc_limit"] >= 55
        fb = self.fallback if not active else None
        crit = st["critical_temp"] if st else self.file_cfg["critical_temp"]
        locked = self.settings["tdp_locked"]
        bat_w = None if self.on_ac else round(self.load + 4.2, 1)
        return {
            "version": VERSION, "ts": now, "temps": temps, "temp_c": temp_c,
            "fan_rpm": self.rpm, "fan1_rpm": self.rpm, "level": level, "speed": self.rpm,
            "fan_control_available": self.fan_control_available, "fan_enabled": self.fan_control_available,
            "fan_status": self.fan_status(),
            "cpu_mhz_avg": int(1400 + self.load * 110), "gpu_mhz": 400 if self.load < 9 else 1200,
            "gpu_busy": int(min(99, max(0, self.load * 2.5 - 10))), "loadavg1": round(self.load / 6.5, 2),
            "governor": "schedutil", "boost": 1,
            "on_ac": self.on_ac, "battery_pct": 79 if self.on_ac else 64,
            "battery_status": "Not charging" if self.on_ac else "Discharging", "battery_watts": 0.0 if self.on_ac else bat_w,
            "daemon_active": active, "daemon_enabled": self.daemon_enabled, "state": st,
            "state_age_s": None if age is None else round(age, 2), "mode": mode,
            "manual_fallback": fb is not None, "manual_critical": bool(fb and fb["critical"]),
            "manual_fallback_level": fb["level"] if fb else None,
            "manual_fallback_remaining_s": max(0, int(fb["until"] - self.t)) if fb else None,
            "smu": smu, "tdp": smu["tdp"], "power": smu["power"], "edc": smu["edc"], "edc_limit": smu["edc_limit"],
            "tdc": smu["tdc"], "tdc_limit": smu["tdc_limit"], "thm_limit": smu["thm_limit"],
            "tdp_requested": self.settings["tdp_requested"], "tdp_locked": locked,
            "tdp_lock_paused_thermal": bool(locked and (temp_c is None or temp_c >= crit - 3)),
            "apply_tdp_at_startup": self.settings["apply_tdp_at_startup"], "startup_tdp_skipped": None,
            "vrm_unlocked": vrm_hw, "vrm_desired": self.vrm_desired,
            "tdp_max": self.tdp_max, "tdp_max_vrm_unlocked": self.tdp_max_vrm,
            "freeze_config_warning": bool(vrm_hw and smu["tdp"] is not None and smu["tdp"] >= 30),
            "critical_temp": crit, "config_mtime": self.file_mtime,
            "hold_default_seconds": self.settings["ui"]["hold_default_seconds"],
        }

    # ── §14 /api/history (fanlib.get_history) ──
    def history(self, minutes):
        out = {"samples": [], "events": []}
        if not self.daemon_active:
            return out
        cutoff = time.time() - minutes * 60
        backend = list(self.backend)
        bt = [b["t"] for b in backend]
        for ts, raw, fast, slow, level, rpm, ac in self.samples:
            if ts < cutoff:
                continue
            k = bisect.bisect_left(bt, ts)
            best = None
            for j in (k - 1, k):
                if 0 <= j < len(bt) and abs(bt[j] - ts) <= 5 and (best is None or abs(bt[j] - ts) < abs(bt[best] - ts)):
                    best = j
            b = backend[best] if best is not None else {}
            out["samples"].append({"t": ts, "cpu": b.get("cpu"), "igpu": b.get("igpu"), "ctrl": raw, "fast": fast,
                                   "slow": slow, "rpm": rpm, "level": level, "ac": ac, "tdp": b.get("tdp"),
                                   "power": b.get("power")})
        out["events"] = [{"t": t, "kind": k, "detail": d} for t, k, d in self.events if t >= cutoff]
        return out

    def log_text(self, lines):
        rows = [l if self.log_nul else l.replace("\x00", "") for l in self.log_lines]
        rows = [r for r in rows if r.strip()][-lines:]
        return "\n".join(rows) + ("\n" if rows else "")

    def settings_view(self):
        s = copy.deepcopy(self.settings)
        s["autostart_enabled"] = self.autostart
        return s

    # ── §14 POST behaviour (reply shapes as in fanlib) ──
    def post(self, path, body):
        fn = {
            "/api/fan/hold": lambda: self.p_hold(body.get("level"), body.get("seconds")),
            "/api/fan/resume": self.p_resume,
            "/api/fan/set": lambda: self.p_hold(body.get("level"), self.settings["ui"]["hold_default_seconds"]),
            "/api/daemon/start": lambda: self.p_daemon("start"),
            "/api/daemon/stop": lambda: self.p_daemon("stop"),
            "/api/daemon/restart": lambda: self.p_daemon("restart"),
            "/api/tdp/set": lambda: self.p_tdp_set(body.get("tdp")),
            "/api/tdp/lock": lambda: self.p_tdp_lock(body.get("locked")),
            "/api/tdp/vrm_unlock": lambda: self.p_vrm(body.get("unlocked")),
            "/api/tdp/restore_stock": self.p_restore_stock,
            "/api/config": lambda: self.p_config(body),
            "/api/settings": lambda: self.p_settings(body),
            "/api/alert/test": self.p_alert_test,
        }.get(path)
        return None if fn is None else fn()

    @staticmethod
    def err(msg, **extra):
        return {"success": False, "error": msg, **extra}

    def p_hold(self, level, seconds):
        if _is_int(level) and 0 <= level <= 7:
            level = str(level)
        if not isinstance(level, str) or level not in RANK:
            return self.err("Unknown fan level; use 0-7, auto, disengaged or full-speed.")
        if seconds is None:
            seconds = self.settings["ui"]["hold_default_seconds"]
        secs = _parse_int(seconds)
        if secs is None or secs < 0:
            return self.err("Hold duration must be a whole number of seconds (0 = until resumed).")
        secs = min(secs, 14400)
        if self.daemon_active:
            r = self.cmd_hold(level, secs)
            if r["ok"]:
                return {"success": True, "error": None, "fallback": False, "state": r["state"]}
            return self.err(r["message"], fallback=False, code=r["error"])
        if self.scenario == "stale":
            return self.err("The daemon is running but its control socket is unreachable; refusing to write the fan "
                            "behind its back.", fallback=False, code="socket_unreachable")
        if level == "auto":
            return self.p_resume()
        if not self.fan_control_available:
            return self.err("Fan control is not available: /proc/acpi/ibm/fan offers no 'level' command "
                            "(thinkpad_acpi must be loaded with fan_control=1).", fallback=True, code="fan_control_unavailable")
        temp = self.raw
        if level == "0" and (temp is None or temp >= FAN_OFF_MAX_TEMP):
            now_txt = f"now {temp:.0f} °C" if temp is not None else "temperature unreadable"
            return self.err(f"Fan off is only allowed below {FAN_OFF_MAX_TEMP} °C ({now_txt}).", fallback=True, code="too_hot_for_fan_off")
        crit = self.cfg["critical_temp"]
        if temp is not None and temp >= crit and RANK[level] < 8:
            return self.err(f"{temp:.0f} °C is at or above the {crit} °C critical limit; without the daemon only "
                            "Maximum can be held right now.", fallback=True, code="critical")
        hold_s = self.cfg["override_max_seconds"] if secs == 0 else min(secs, self.cfg["override_max_seconds"])
        self.fallback = {"level": level, "until": self.t + hold_s, "critical": False}
        self.level_proc = level
        return {"success": True, "error": None, "fallback": True, "seconds": hold_s}

    def p_resume(self):
        if self.daemon_active:
            return {"success": True, "error": None, "fallback": False, "state": self.cmd_resume()["state"]}
        if self.scenario == "stale":
            return self.err("The daemon is running but its control socket is unreachable.", fallback=False, code="socket_unreachable")
        self.fallback = None
        self.level_proc = "auto"
        return {"success": True, "error": None, "fallback": True}

    def p_daemon(self, action):
        if action in ("stop", "restart"):
            if self.daemon_active or self.scenario == "stale":
                self.log("Daemon stopping — returning fan to firmware control")
            self.daemon_active = False
            self.override = None
            self.critical = False
            self.level = self.level_proc = "auto"
            self.frozen = None
            self.hung_at = None
            self.scenario = "curve" if self.scenario == "stale" else self.scenario
        if action in ("start", "restart"):
            if not self.fan_control_available:
                return self.err('See "systemctl status thinkpad-fan-control.service" and '
                                '"journalctl -xeu thinkpad-fan-control.service" for details.')
            if not self.daemon_active:
                self.daemon_active = True
                self.pid += 1
                self.fallback = None
                base = self.raw if self.raw is not None else self.T
                self.t_fast = self.t_slow = base
                self.reseat()
                self.level = self.level_proc
                self.level_since = self.t
                self.reason = "curve"
                self._daemon_start_log()
        return {"success": True, "error": None}

    def p_tdp_set(self, watts):
        w = _parse_int(watts, digits=3)
        if w is None:
            return self.err("TDP must be a whole number of watts.")
        if not 5 <= w <= self.tdp_max:
            return self.err(f"TDP must be between 5 and {self.tdp_max} W.")
        if self.vrm_desired and w > self.tdp_max_vrm:
            return self.err(f"With the VRM current limits unlocked the TDP is capped at {self.tdp_max_vrm} W.")
        if not self.smu_ok:
            return self.err("sudo: a password is required")
        self.smu["tdp"] = w
        self.settings["tdp_requested"] = w
        return {"success": True, "error": None, "tdp_requested": w}

    def p_tdp_lock(self, locked):
        if not isinstance(locked, bool):
            return self.err("'locked' must be true or false.")
        if locked and self.settings["tdp_requested"] is None:
            if not self.smu_ok:
                return self.err("Cannot lock: no TDP has been requested and the SMU reading is unavailable. Apply a TDP first.")
            self.settings["tdp_requested"] = int(self.smu["tdp"])
        self.settings["tdp_locked"] = locked
        return {"success": True, "error": None, "tdp_locked": locked, "tdp_requested": self.settings["tdp_requested"]}

    def _current_watts(self):
        req = self.settings["tdp_requested"]
        if req is not None:
            return req, None
        if not self.smu_ok:
            return None, "The SMU reading is unavailable, so the current TDP is unknown; apply a TDP first."
        return int(self.smu["tdp"]), None

    def p_vrm(self, unlocked):
        if not isinstance(unlocked, bool):
            return self.err("'unlocked' must be true or false.")
        watts, why = self._current_watts()
        if watts is None:
            return self.err(why)
        if unlocked and watts > self.tdp_max_vrm:
            return self.err(f"Lower the TDP to {self.tdp_max_vrm} W or below before unlocking")
        if not self.smu_ok:
            return self.err("sudo: a password is required")
        self.vrm_desired = unlocked
        self.smu["edc_limit"], self.smu["tdc_limit"] = (60.0, 42.0) if unlocked else (45.0, 35.0)
        self.smu["tdp"] = watts
        return {"success": True, "error": None, "vrm_desired": unlocked, "tdp": watts}

    def p_restore_stock(self):
        req = self.settings["tdp_requested"]
        watts = max(5, min(req if req is not None else 15, self.tdp_max))
        self.vrm_desired = False
        if not self.smu_ok:
            return self.err("sudo: a password is required")
        self.smu["edc_limit"], self.smu["tdc_limit"], self.smu["tdp"] = 45.0, 35.0, watts
        return {"success": True, "error": None, "tdp": watts, "vrm_desired": False}

    def p_config(self, body):
        if self.fail_saves:
            return self.err("fan-config-save.sh exited 1")
        cfg, rejected = sanitize(body, self.file_cfg)
        self.file_cfg = cfg
        self.file_mtime = round(time.time(), 6)    # the daemon picks it up on its next tick
        self.log("Config updated from GUI")
        return {"success": True, "error": None, "config": copy.deepcopy(cfg), "rejected": rejected}

    def p_settings(self, body):
        errors, ignored, patch = [], [], {}
        for k in body:
            if k not in ("ui", "apply_tdp_at_startup", "autostart_enabled"):
                ignored.append(k)
        if "ui" in body:
            ui = body["ui"]
            if not isinstance(ui, dict):
                errors.append("'ui' must be an object.")
            else:
                for k, v in ui.items():
                    if k not in DEFAULT_SETTINGS["ui"]:
                        ignored.append(f"ui.{k}")
                    elif k == "hold_default_seconds" and not (_is_int(v) and 0 <= v <= 14400):
                        errors.append("ui.hold_default_seconds must be a whole number of seconds between 0 and 14400.")
                    elif k == "history_range_min" and not (_is_int(v) and 1 <= v <= 30):
                        errors.append("ui.history_range_min must be 1 to 30 minutes.")
                    elif k in ("log_filter", "view") and not (isinstance(v, str) and _UI_WORD.match(v)):
                        errors.append(f"ui.{k} must be a short lowercase identifier.")
                    else:
                        patch.setdefault("ui", {})[k] = v
        if "apply_tdp_at_startup" in body:
            if isinstance(body["apply_tdp_at_startup"], bool):
                patch["apply_tdp_at_startup"] = body["apply_tdp_at_startup"]
            else:
                errors.append("'apply_tdp_at_startup' must be true or false.")
        if "autostart_enabled" in body and not isinstance(body["autostart_enabled"], bool):
            errors.append("'autostart_enabled' must be true or false.")
        if errors:
            return self.err(" ".join(errors), settings=self.settings_view())
        if isinstance(body.get("autostart_enabled"), bool):
            self.autostart = body["autostart_enabled"]
        self.settings["ui"].update(patch.pop("ui", {}))
        self.settings.update(patch)
        return {"success": True, "error": None, "settings": self.settings_view(), "ignored": ignored}

    def p_alert_test(self):
        # Never plays anything: only the rate limits and reply shapes are mimicked.
        if time.time() - self.last_test_alert < 10:
            if self.daemon_active:
                return self.err("One test alert per 10 s.", fallback=False, code="rate_limited")
            return self.err("Please wait a few seconds between test alerts.", code="rate_limited")
        self.last_test_alert = time.time()
        if self.daemon_active:
            self.log("Test alert (uid 1000)")
        return {"success": True, "error": None, "fallback": not self.daemon_active}


# ── HTTP layer (the §14 gates, like fanlib.Handler) ─────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 10
    server_version = "fanctl-mock/" + VERSION
    sys_version = ""
    sim: Sim = None
    verbose = False

    def log_message(self, fmt, *args):
        if self.verbose:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _send(self, body, ctype, code=200, csp=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if csp:
            self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, allow_nan=False), "application/json; charset=utf-8", code)

    def _empty(self, code):
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _reject(self, code, msg):
        self.close_connection = True
        self._json({"success": False, "error": msg}, code)

    def _hosts(self):
        p = self.server.server_port
        return (f"127.0.0.1:{p}", f"localhost:{p}")

    def _unreachable(self):
        """Scenario 'unreachable': accept, then never answer (like a wedged backend) until the client gives up."""
        sim = self.sim
        if sim.scenario != "unreachable" or time.time() - sim.serve_t0 < sim.unreachable_after:
            return False
        end = time.time() + 60
        while time.time() < end:
            r, _, _ = select.select([self.connection], [], [], 0.5)
            if r:
                try:
                    if not self.connection.recv(1, 2):   # MSG_PEEK: b"" = the client closed
                        break
                except OSError:
                    break
        self.close_connection = True
        return True

    def do_OPTIONS(self):
        if self._unreachable():
            return
        if (self.headers.get("Host") or "").strip().lower() not in self._hosts():
            return self._empty(421)
        self._reject(403, "OPTIONS is not supported.")

    def _method_not_allowed(self):
        self.close_connection = True
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD, POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_PUT = do_DELETE = do_PATCH = _method_not_allowed

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self._unreachable():
            return
        if (self.headers.get("Host") or "").strip().lower() not in self._hosts():
            return self._empty(421)
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        path = url.path
        if path in ("/", "/index.html"):
            try:
                body = INDEX_HTML.read_bytes()
            except OSError:
                return self._send("index.html is missing\n", "text/plain; charset=utf-8", 404)
            return self._send(body, "text/html; charset=utf-8", csp=True)
        sim = self.sim
        with sim.lock:
            if path == "/api/status":
                return self._json(sim.status())
            if path == "/api/config":
                return self._json(copy.deepcopy(sim.file_cfg))
            if path == "/api/history":
                m = _parse_int((q.get("minutes") or ["30"])[0], digits=4)
                return self._json(sim.history(30 if m is None else max(1, min(30, m))))
            if path == "/api/log":
                n = _parse_int((q.get("lines") or ["200"])[0])
                return self._send(sim.log_text(200 if n is None else max(1, min(2000, n))), "text/plain; charset=utf-8")
            if path == "/api/settings":
                return self._json(sim.settings_view())
        if path.startswith("/api/"):
            return self._json({"success": False, "error": "Unknown API path."}, 404)
        self._empty(404)

    def do_POST(self):
        if self._unreachable():
            return
        if (self.headers.get("Host") or "").strip().lower() not in self._hosts():
            return self._empty(421)
        if not (self.headers.get("Content-Type") or "").strip().lower().startswith("application/json"):
            return self._reject(415, "POST bodies must be sent as application/json.")
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() not in tuple("http://" + h for h in self._hosts()):
            return self._reject(403, "Cross-origin requests are not accepted.")
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs.strip().lower() not in ("same-origin", "none"):
            return self._reject(403, "Cross-site requests are not accepted.")
        try:
            n = int((self.headers.get("Content-Length") or "0").strip())
        except ValueError:
            return self._reject(400, "Content-Length is not a number.")
        if n < 0:
            return self._reject(400, "Content-Length is negative.")
        if n > MAX_BODY:
            return self._reject(413, f"The request body is larger than {MAX_BODY} bytes.")
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (UnicodeDecodeError, ValueError):
            return self._json({"success": False, "error": "The request body is not valid JSON."}, 400)
        if not isinstance(body, dict):
            return self._json({"success": False, "error": "The request body must be a JSON object."}, 400)
        path = urlsplit(self.path).path
        with self.sim.lock:
            try:
                result = self.sim.post(path, body)
            except Exception as e:                 # noqa: BLE001 — a mock must not die on a bad body
                result = {"success": False, "error": f"Internal error: {e}"}
        if result is None:
            return self._json({"success": False, "error": "Unknown API path."}, 404)
        self._json(result)


SCENARIOS = ["curve", "hold", "hold_suspended", "critical", "daemon_down", "unreachable", "smu_missing",
             "freeze_config", "sensor_missing", "fan_control_unavailable", "manual_unprotected", "stale", "battery"]


def ticker(sim, stop):
    """Advance the simulation in 1 s steps so it tracks the wall clock."""
    while not stop.wait(0.25):
        with sim.lock:
            while sim.t + 1.0 <= time.time():
                sim.step()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=7150, help="127.0.0.1 port (default 7150; 7070 is refused)")
    ap.add_argument("--scenario", choices=SCENARIOS, default="curve")
    ap.add_argument("--seed-minutes", type=int, default=20, help="minutes of history simulated before serving (default 20)")
    ap.add_argument("--backend-age", type=float, default=5.0,
                    help="minutes the backend's own 2 s sampler has run (older samples lack cpu/igpu/tdp; default 5)")
    ap.add_argument("--unreachable-after", type=float, default=30.0, help="scenario 'unreachable': seconds before it stops answering")
    ap.add_argument("--rng-seed", type=int, default=7)
    ap.add_argument("--fail-saves", action="store_true", help="POST /api/config fails (save-failure path)")
    ap.add_argument("--log-nul", action="store_true", help="leave NUL padding in /api/log (the real backend strips it)")
    ap.add_argument("--smu-tdp", type=int, default=None, help="STAPM limit (W) the SMU reports at start")
    ap.add_argument("--verbose", action="store_true", help="log every request to stderr")
    args = ap.parse_args(argv)
    if args.port == 7070:
        print("refusing to bind 7070: that is the real backend's port", file=sys.stderr)
        return 2
    if not 1 <= args.port <= 65535:
        print(f"invalid port {args.port}", file=sys.stderr)
        return 2
    sim = Sim(args.scenario, args.seed_minutes, args.rng_seed, args.backend_age, args.unreachable_after,
              args.fail_saves, args.log_nul, args.smu_tdp)
    Handler.sim = sim
    Handler.verbose = args.verbose
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        print(f"cannot bind 127.0.0.1:{args.port}: {e}", file=sys.stderr)
        return 1
    httpd.daemon_threads = True
    with sim.lock:
        sim.serve_t0 = time.time()                 # "unreachable" counts from here
    stop = threading.Event()
    threading.Thread(target=ticker, args=(sim, stop), daemon=True, name="sim").start()
    print(f"mock backend: http://127.0.0.1:{args.port}/  scenario={args.scenario}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
