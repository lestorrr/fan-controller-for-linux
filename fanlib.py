#!/usr/bin/env python3
"""
ThinkPad Fan Control — shared backend (v2; docs/CONTRACT.md §9, §14, §15).

Everything that touches hardware, the daemon or the per-user settings lives
here, so app.py (GTK/WebKit shell) and server.py (plain browser mode) are
thin wrappers over the same code. This module never imports GTK.

The fan belongs to the root daemon (thinkpad-fan-controld). The backend talks
to it over its control socket and reads its state.json/history.json. Only
when the daemon is not running does the backend write the fan directly
(through fan-set-level.sh, with the EC watchdog armed at 120 s) and then it
supervises that write itself with a keep-alive thread that also forces
"disengaged" at critical_temp.

Public names used by app.py / server.py
---------------------------------------
    VERSION, PORT, DRY_RUN, VALID_LEVELS
    make_server(port)          bind 127.0.0.1:port; raises OSError when taken
    start_workers()            session start (§9 last_clean_exit, startup TDP
                               rule), TDP lock loop, history sampler
    start_background(port)     make_server + serve thread + start_workers()
    release_all()              the shared do_quit release path (§16); idempotent
    get_status()               the §15 status object
    mode(daemon_active, level, state)   the single mode derivation (tray too)
    fan_hold(level, seconds)   socket hold, or the daemon-less fallback
    fan_resume()               socket resume, or stop the fallback + "auto 0"
    restore_stock()            tdp_requested (or 15) W with stock VRM
    set_visibility(bool)       SMU poll cadence: 10 s visible / 30 s hidden
    last_resume_age()          seconds since this process last asked to resume
    fmt_duration(seconds)      "15 min" / "until resumed" for menu labels
    FALLBACK                   the keep-alive (FALLBACK.active,
                               FALLBACK.manual_critical, FALLBACK.last_end)

Environment overrides (tests only; production uses the defaults)
    FANCTL_DRY_RUN=1        skip every sudo/socket/sound actuation and report
                            success; sensor reads stay real
    FANCTL_RUNTIME_DIR      state.json / history.json / control.sock directory
    FANCTL_SETTINGS_FILE    per-user settings.json
    FANCTL_AUTOSTART_FILE   ~/.config/autostart/fan-control.desktop
    FANCTL_ENV_FILE         /etc/thinkpad-fan-control/daemon.env
    FANCTL_LOG_FILE         /var/log/thinkpad-fan-control.log
    FANCTL_CONFIG_FILE, FANCTL_HTML_FILE, FANCTL_FAN_PROC (read-only inputs)

Every path is a module attribute read at call time, so a test may also
assign fanlib.X = ... after import.
"""

import bisect
import collections
import copy
import errno
import glob
import json
import os
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "2.0.0"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_HOME = os.path.expanduser("~")

# ─────────────────────────────────────────────────────────────────────────────
# Paths and constants
# ─────────────────────────────────────────────────────────────────────────────

PROD_RUNTIME_DIR = "/run/thinkpad-fan-control"

FAN_PROC       = os.environ.get("FANCTL_FAN_PROC") or "/proc/acpi/ibm/fan"
LOG_FILE       = os.environ.get("FANCTL_LOG_FILE") or "/var/log/thinkpad-fan-control.log"
CONFIG_FILE    = os.environ.get("FANCTL_CONFIG_FILE") or "/etc/thinkpad-fan-control/config.json"
ENV_FILE       = os.environ.get("FANCTL_ENV_FILE") or "/etc/thinkpad-fan-control/daemon.env"
RUNTIME_DIR    = os.environ.get("FANCTL_RUNTIME_DIR") or PROD_RUNTIME_DIR
SETTINGS_FILE  = os.environ.get("FANCTL_SETTINGS_FILE") or os.path.join(
    _HOME, ".config", "thinkpad-fan-control", "settings.json")
AUTOSTART_FILE = os.environ.get("FANCTL_AUTOSTART_FILE") or os.path.join(
    _HOME, ".config", "autostart", "fan-control.desktop")
HTML_FILE      = os.environ.get("FANCTL_HTML_FILE") or os.path.join(SCRIPT_DIR, "index.html")
FALLBACK_WAV   = "/usr/local/share/thinkpad-fan-control/alert.wav"
REPO_WAV       = os.path.join(SCRIPT_DIR, "alert.wav")

FAN_SET   = "/usr/local/bin/fan-set-level.sh"
TDP_SET   = "/usr/local/bin/ryzenadj-set-tdp.sh"
CFG_SAVE  = "/usr/local/bin/fan-config-save.sh"
RYZENADJ  = "/usr/local/bin/ryzenadj"
SYSTEMCTL = "/usr/bin/systemctl"
SERVICE   = "thinkpad-fan-control.service"

PORT = 7070

# FANCTL_DRY_RUN=1: every sudo / socket / sound actuation is skipped and
# reported as success (§14). Read once: flipping it at runtime must not be
# able to turn a test process into one that writes hardware.
DRY_RUN = os.environ.get("FANCTL_DRY_RUN") == "1"

VALID_LEVELS = frozenset({"auto", "disengaged", "full-speed"} | {str(i) for i in range(8)})
LEVEL_RANK = {**{str(i): i for i in range(8)}, "disengaged": 8, "full-speed": 8, "auto": -1}

MAX_BODY             = 65536   # §14: larger Content-Length → 413 before reading
STARTUP_TDP_MAX      = 25      # §9: never re-applied unattended above this
TDP_ABS_MIN          = 5       # the wrapper's regex floor
TDP_ABS_MAX          = 40      # the wrapper's regex ceiling
TDP_DEFAULT_MAX      = 35      # §2/§15 defaults when daemon.env lacks a value
TDP_DEFAULT_MAX_VRM  = 30
TDP_RESTORE_DEFAULT  = 15      # §14 restore_stock: "tdp_requested or 15"
TDP_LOCK_TICK_S      = 5
TDP_LOCK_FLOOR_S     = 20      # §14: never re-apply more than once per 20 s
TDP_THERMAL_MARGIN   = 3       # §14: lock paused at critical_temp - 3
SUSPEND_GAP_S        = 5       # §14: wall_delta - mono_delta above this = resume
FALLBACK_WATCHDOG    = 120     # EC watchdog armed for a daemon-less hold
FALLBACK_REFRESH_S   = 30      # §14: re-write the held level every 30 s
# Temperature check cadence inside the keep-alive. Tctl can jump 11 °C in
# one second on this unit and a check is two sysfs reads, so 2 s is cheap.
FALLBACK_TICK_S      = 2
# After a fallback critical episode the die must stay below the exit
# threshold this long (raw samples, no EMA here) before firmware gets it back.
FALLBACK_COOL_CONFIRM_S = 10
FAN_OFF_MAX_TEMP     = 55      # §7: level "0" refused at or above this
OVERRIDE_DEFAULT_S   = 7200    # §3 override_max_seconds default
OVERRIDE_HARD_MAX_S  = 14400   # §3 override_max_seconds upper bound
SOCKET_TIMEOUT_S     = 3.0
SMU_TTL_VISIBLE      = 10.0
SMU_TTL_HIDDEN       = 30.0
VRM_UNLOCKED_EDC_A   = 55      # §15: stock EDC 45 A, unlocked 60 A
FREEZE_WARN_TDP      = 30      # §15 freeze_config_warning threshold
HISTORY_MAX_MIN      = 30
HISTORY_MATCH_S      = 5       # §14: nearest backend sample within 5 s
LOG_DEFAULT_LINES    = 200
LOG_MAX_LINES        = 2000
# The log carries NUL padding from past hard crashes: one "line" can be
# megabytes of NULs, so the backwards reader is bounded by raw bytes read.
LOG_MAX_READ_BYTES   = 4 * 1024 * 1024
BACKEND_SAMPLE_S     = 2
BACKEND_SAMPLES_MAX  = 900     # 30 min at 2 s (§14)

CSP = "default-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:"

# ui.view / ui.log_filter are echoed back to the page, which may use them in
# selectors or class names: keep them to a boring identifier charset.
_UI_WORD = re.compile(r"^[a-z0-9_-]{1,32}$")

# Mirror of the daemon's §3 DEFAULTS, used only to fill keys for display when
# config.json is missing or still v1. The daemon's sanitize() is the only
# validator; the backend never writes config.json itself.
DEFAULT_CONFIG = {
    "schema": 2,
    "sample_interval": 1,
    "smoothing_up_s": 8,
    "smoothing_down_s": 30,
    "dwell_down_s": 45,
    "step_down_spacing_s": 10,
    "hysteresis": 6,
    "critical_temp": 90,
    "critical_exit_margin": 8,
    "critical_exit_hold_s": 60,
    "watchdog": 60,
    "override_max_seconds": 7200,
    "override_ceiling_temp": 82,
    "curve": [
        {"temp": 0,  "level": "auto"},
        {"temp": 50, "level": "4"},
        {"temp": 60, "level": "6"},
        {"temp": 70, "level": "7"},
        {"temp": 80, "level": "disengaged", "down": 72},
    ],
    "battery_curve": [
        {"temp": 0,  "level": "auto"},
        {"temp": 60, "level": "3"},
        {"temp": 70, "level": "5"},
        {"temp": 80, "level": "7"},
        {"temp": 87, "level": "disengaged", "down": 78},
    ],
    "use_battery_curve": True,
    "alerts_enabled": True,
    "alert_sound": "/home/jhnlstrlclcn/Music/SYSTEM SOUND/90c.mp3",
    "alert_cooldown": 300,
}

DEFAULT_SETTINGS = {
    "schema": 1,
    "tdp_requested": None,
    "tdp_locked": False,
    "apply_tdp_at_startup": False,
    "last_clean_exit": True,
    "ui": {
        "hold_default_seconds": 900,
        "history_range_min": 15,
        "log_filter": "all",
        "view": "overview",
    },
}


def state_file():
    return os.path.join(RUNTIME_DIR, "state.json")


def history_file():
    return os.path.join(RUNTIME_DIR, "history.json")


def control_sock():
    return os.path.join(RUNTIME_DIR, "control.sock")


def _runtime_is_production():
    return os.path.realpath(RUNTIME_DIR) == PROD_RUNTIME_DIR


# ─────────────────────────────────────────────────────────────────────────────
# Logging (stdout: a terminal shows it, the .desktop launcher discards it)
# ─────────────────────────────────────────────────────────────────────────────

_log_lock = threading.Lock()
_log_last = {}


def _log(msg):
    with _log_lock:
        print(f"[{time.strftime('%H:%M:%S')}] fanlib: {msg}", flush=True)


def _log_once(key, msg, every=60.0):
    """Rate-limit a repeating line (a lock loop that keeps skipping, a dead sensor)."""
    now = time.monotonic()
    with _log_lock:
        if now - _log_last.get(key, -1e9) < every:
            return
        _log_last[key] = now
    _log(msg)


# ─────────────────────────────────────────────────────────────────────────────
# Low-level file access
# ─────────────────────────────────────────────────────────────────────────────

def read_file(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except (OSError, TypeError, ValueError):
        return None


def read_int(path, default=None):
    v = read_file(path) if path else None
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _num(v):
    """int/float that is neither bool nor NaN/inf, else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _loads_lenient(text):
    """json.loads for files written by the daemon: NaN/Infinity become null.

    Python's json accepts those non-standard constants, and one of them in
    state.json would otherwise travel into /api/status, where the strict
    encoder (allow_nan=False) turns the whole reply into a 500 and the
    dashboard into "backend unreachable".
    """
    return json.loads(text, parse_constant=lambda _c: None)


def _reject_constant(c):
    raise ValueError(f"non-standard JSON constant {c}")


def _loads_strict(text):
    """json.loads for request bodies: NaN/Infinity are invalid JSON (→ 400)."""
    return json.loads(text, parse_constant=_reject_constant)


# ─────────────────────────────────────────────────────────────────────────────
# hwmon discovery
#
# hwmon numbering follows module probe order and does shuffle between boots,
# so sensors are looked up by driver name, never by hwmonN. Misses are cached
# for 30 s: an absent sensor (Wi-Fi off) must not force a /sys rescan on
# every poll from two pollers.
# ─────────────────────────────────────────────────────────────────────────────

class HwmonIndex:
    NEGATIVE_TTL = 30.0

    def __init__(self):
        self._by_name = {}
        self._scan_at = -1e9
        self._lock = threading.Lock()

    @staticmethod
    def _scan():
        found = {}
        for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            name = read_file(os.path.join(d, "name"))
            if name and name not in found:
                found[name] = d
        return found

    def _resolve(self, matcher):
        with self._lock:
            for name, d in self._by_name.items():
                # Re-check the name file: a module reload can renumber hwmonN.
                if matcher(name) and read_file(os.path.join(d, "name")) == name:
                    return d
            now = time.monotonic()
            if now - self._scan_at < self.NEGATIVE_TTL:
                return None
            self._by_name = self._scan()
            self._scan_at = now
            for name, d in self._by_name.items():
                if matcher(name):
                    return d
            return None

    def dir_for(self, name):
        return self._resolve(lambda n: n == name)

    def dir_for_prefix(self, prefix):
        # The Wi-Fi hwmon is "iwlwifi_<phy>" (iwlwifi_1 here, not iwlwifi_1_0).
        return self._resolve(lambda n: n.startswith(prefix))

    def attr(self, name, attr, prefix=False):
        d = self.dir_for_prefix(name) if prefix else self.dir_for(name)
        return os.path.join(d, attr) if d else None

    def temp_float(self, name, attr="temp1_input", prefix=False):
        """°C with millidegree precision, or None (absent, or outside (0, 150) per §4)."""
        raw = read_int(self.attr(name, attr, prefix=prefix))
        if raw is None:
            return None
        v = raw / 1000.0
        return v if 0 < v < 150 else None

    def temp(self, name, attr="temp1_input", prefix=False):
        """Whole °C (§15 'ints'), or None."""
        v = self.temp_float(name, attr, prefix=prefix)
        return None if v is None else int(round(v))


HWMON = HwmonIndex()


# ─────────────────────────────────────────────────────────────────────────────
# Sensors (real even in dry-run mode)
# ─────────────────────────────────────────────────────────────────────────────

def read_temps():
    return {
        "cpu":  HWMON.temp("k10temp"),                  # Tctl
        "igpu": HWMON.temp("amdgpu"),                   # Vega edge
        "nvme": HWMON.temp("nvme"),                     # Composite
        "wifi": HWMON.temp("iwlwifi", prefix=True),
    }


def read_control_temp_raw():
    """max(Tctl, edge) as a float, like the daemon's raw; None when neither reads.

    Used whenever the daemon is not active (keep-alive, TDP thermal pause).
    Floats matter at the edges: an int-floored 89.9 would read as 89.
    """
    vals = [v for v in (HWMON.temp_float("k10temp"), HWMON.temp_float("amdgpu")) if v is not None]
    return max(vals) if vals else None


def read_fan_rpm():
    """One physical fan; fan2_input duplicates fan1_input on the T495."""
    return read_int(HWMON.attr("thinkpad", "fan1_input"))


def read_fan_proc():
    """
    Parse /proc/acpi/ibm/fan.

    `status` is the raw EC word: "disabled" only means the fan register is 0
    (level 0, fan stopped), NOT that control is off. Whether the GUI can
    control the fan is the presence of a "commands:" line offering `level`,
    which thinkpad_acpi prints only when loaded with fan_control=1.
    """
    out = {"readable": False, "level": None, "speed": None,
           "status": None, "control_available": False}
    text = read_file(FAN_PROC)
    if text is None:
        return out
    out["readable"] = True
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        key, words = key.strip(), rest.split()
        if not words:
            continue
        if key == "status":
            out["status"] = words[0]
        elif key == "speed":
            try:
                out["speed"] = int(words[0])
            except ValueError:
                pass
        elif key == "level":
            out["level"] = words[0]
        elif key == "commands" and words[0] == "level":
            out["control_available"] = True
    return out


def read_power():
    """AC/battery state; fields are None where the kernel exposes nothing."""
    ac = read_file("/sys/class/power_supply/AC/online")
    bat = "/sys/class/power_supply/BAT0"
    watts = None
    power_now = read_int(os.path.join(bat, "power_now"))
    if power_now is not None:
        watts = round(abs(power_now) / 1e6, 1)       # 0 W on AC with a full battery is real
    else:
        cur = read_int(os.path.join(bat, "current_now"))
        vol = read_int(os.path.join(bat, "voltage_now"))
        if cur is not None and vol is not None:
            watts = round(abs(cur) * vol / 1e12, 1)
    return {
        "on_ac":          (ac == "1") if ac is not None else True,   # missing → AC, like the daemon
        "battery_pct":    read_int(os.path.join(bat, "capacity")),
        "battery_status": read_file(os.path.join(bat, "status")),
        "battery_watts":  watts,
    }


def read_cpu_mhz_avg():
    total, n = 0.0, 0
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("cpu MHz"):
                    try:
                        total += float(line.split(":", 1)[1])
                        n += 1
                    except (IndexError, ValueError):
                        pass
    except OSError:
        return None
    return int(round(total / n)) if n else None


def read_gpu_mhz():
    hz = read_int(HWMON.attr("amdgpu", "freq1_input"))     # sclk in Hz
    return int(round(hz / 1e6)) if hz else None


_gpu_busy_path = [None]


def read_gpu_busy():
    p = _gpu_busy_path[0]
    if p is None or (p and not os.path.exists(p)):
        cands = sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"))
        p = _gpu_busy_path[0] = cands[0] if cands else ""
    return read_int(p) if p else None


def read_loadavg1():
    v = read_file("/proc/loadavg")
    try:
        return float(v.split()[0])
    except (AttributeError, IndexError, ValueError):
        return None


def read_governor():
    return read_file("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")


def read_boost():
    """cpufreq boost flag (int), or None where the driver exposes none."""
    for p in ("/sys/devices/system/cpu/cpu0/cpufreq/boost",
              "/sys/devices/system/cpu/cpufreq/boost"):
        v = read_int(p)
        if v is not None:
            return v
    return None


# ─────────────────────────────────────────────────────────────────────────────
# daemon.env (§2): the root-side TDP caps, mirrored for validation and display
#
# ryzenadj-set-tdp.sh enforces the same numbers as root; the backend reads
# them only so the UI never offers a setting the wrapper would refuse.
# ─────────────────────────────────────────────────────────────────────────────

_env_cache = {"key": None, "vals": {}}
_env_lock = threading.Lock()
_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_ENV_DQ = re.compile(r'"((?:[^"\\]|\\.)*)"')
_ENV_SQ = re.compile(r"'([^']*)'")


def _unquote_env(v):
    """
    The value bash assigns for `KEY=<v>` in the forms install.sh writes
    (bare word, "double-quoted", 'single-quoted'), including a trailing
    ` # comment`. ryzenadj-set-tdp.sh *sources* this file, so reading it any
    other way could let the two sides disagree about a cap. Anything more
    exotic comes back unparsed and then fails the caller's digit check,
    which means "use the default" on both sides.
    """
    v = v.strip()
    for rx, unescape in ((_ENV_DQ, True), (_ENV_SQ, False)):
        if v[:1] == rx.pattern[0]:
            m = rx.match(v)
            rest = v[m.end():] if m else None
            if m and (not rest or rest[0].isspace()):
                inner = m.group(1)
                return re.sub(r'\\(["\\$`])', r"\1", inner) if unescape else inner
            return v
    # Bare word: bash ends it at the first blank (after which only a comment
    # is legitimate in this file).
    return v.split(None, 1)[0] if v else v


def daemon_env():
    """KEY → value from daemon.env (cached by path + mtime); {} when absent."""
    path = ENV_FILE
    with _env_lock:
        try:
            key = (path, os.stat(path).st_mtime_ns)
        except OSError:
            _env_cache["key"], _env_cache["vals"] = None, {}
            return {}
        if key == _env_cache["key"]:
            return dict(_env_cache["vals"])
        vals = {}
        for line in (read_file(path) or "").splitlines():
            m = _ENV_LINE.match(line.strip())
            if m:
                vals[m.group(1)] = _unquote_env(m.group(2))
        _env_cache["key"], _env_cache["vals"] = key, vals
        return dict(vals)


def tdp_caps():
    """{'tdp_max', 'tdp_max_vrm_unlocked'} exactly as ryzenadj-set-tdp.sh will enforce them."""
    env = daemon_env()

    def cap(name, default):
        # Same acceptance rule as the wrapper (^[1-9][0-9]?$, else default),
        # so the two sides can never disagree about a malformed value.
        v = env.get(name, "")
        return int(v) if re.fullmatch(r"[1-9][0-9]?", v) else default

    tdp_max = min(cap("FANCTL_TDP_MAX", TDP_DEFAULT_MAX), TDP_ABS_MAX)
    # The wrapper checks both caps for an unlocked request, so the effective
    # unlocked cap can never exceed the general one.
    return {"tdp_max": tdp_max,
            "tdp_max_vrm_unlocked": min(cap("FANCTL_TDP_MAX_VRM_UNLOCKED", TDP_DEFAULT_MAX_VRM), tdp_max)}


# ─────────────────────────────────────────────────────────────────────────────
# Per-user settings.json (§9)
#
# Only requested values and explicit opt-ins are persisted. VRM unlock is
# session-only on purpose: persisting it is how a one-off experiment became a
# permanent 30 W + 60 A configuration before the 2026-09-15 GPU hang.
# ─────────────────────────────────────────────────────────────────────────────

_settings_lock = threading.RLock()
_settings = {"path": None, "obj": None}     # loaded lazily, keyed by path so tests can repoint


def _sanitize_settings(raw, base=None):
    """
    Whitelist and range-check a §9 object. A missing or invalid value falls
    back to `base`, then to the defaults, so one bad field never resets the
    others. Unknown keys (vrm_* in particular) are dropped.
    """
    out = _sanitize_settings(base) if isinstance(base, dict) else copy.deepcopy(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return out
    if "tdp_requested" in raw:
        v = raw["tdp_requested"]
        if v is None or (_is_int(v) and TDP_ABS_MIN <= v <= TDP_ABS_MAX):
            out["tdp_requested"] = v
    for key in ("tdp_locked", "apply_tdp_at_startup", "last_clean_exit"):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    ui = raw.get("ui")
    if isinstance(ui, dict):
        errs = _ui_errors(ui)
        for k, v in ui.items():
            if k in out["ui"] and k not in errs:
                out["ui"][k] = v
    return out


def _ui_errors(ui):
    """{key: message} for invalid values in a partial ui object (unknown keys ignored)."""
    errs = {}
    if "hold_default_seconds" in ui:
        v = ui["hold_default_seconds"]
        if not (_is_int(v) and 0 <= v <= OVERRIDE_HARD_MAX_S):
            errs["hold_default_seconds"] = (f"ui.hold_default_seconds must be a whole number of "
                                            f"seconds between 0 and {OVERRIDE_HARD_MAX_S}.")
    if "history_range_min" in ui:
        v = ui["history_range_min"]
        if not (_is_int(v) and 1 <= v <= HISTORY_MAX_MIN):
            errs["history_range_min"] = f"ui.history_range_min must be 1 to {HISTORY_MAX_MIN} minutes."
    for k in ("log_filter", "view"):
        if k in ui:
            v = ui[k]
            if not (isinstance(v, str) and _UI_WORD.match(v)):
                errs[k] = f"ui.{k} must be a short lowercase identifier."
    return errs


def _write_settings(obj):
    path = SETTINGS_FILE
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    tmp = f"{path}.{os.getpid()}.tmp"
    data = (json.dumps(obj, indent=1) + "\n").encode("utf-8")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)             # explicit: never trust the umask
            view = memoryview(data)
            while view:                      # os.write may write less than asked
                view = view[os.write(fd, view):]
            os.fsync(fd)                     # a real disk, unlike /run
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError:
        # Disk full / read-only home: leave no half-written temp file behind.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _settings_locked():
    if _settings["path"] != SETTINGS_FILE or _settings["obj"] is None:
        raw, text = None, read_file(SETTINGS_FILE)
        if text:
            try:
                raw = _loads_lenient(text)
            except (ValueError, RecursionError):
                _log(f"settings.json is not valid JSON, using defaults ({SETTINGS_FILE})")
        _settings["path"], _settings["obj"] = SETTINGS_FILE, _sanitize_settings(raw)
    return _settings["obj"]


def get_settings():
    with _settings_lock:
        return copy.deepcopy(_settings_locked())


def update_settings(patch):
    """Merge a partial §9 object (ui key by key), sanitize, persist; returns a copy.

    Internal: every §9 key is accepted here. The HTTP layer restricts what a
    client may change (apply_settings_patch).
    """
    with _settings_lock:
        cur = _settings_locked()
        merged = copy.deepcopy(cur)
        for k, v in (patch or {}).items():
            if k == "ui" and isinstance(v, dict):
                merged["ui"].update(v)
            else:
                merged[k] = v
        new = _sanitize_settings(merged, base=cur)
        if new != cur or not os.path.exists(SETTINGS_FILE):
            try:
                _write_settings(new)
            except OSError as e:
                _log_once("settings-save", f"settings save failed: {e}")
        _settings["obj"] = new
        return copy.deepcopy(new)


def reload_settings():
    """Forget the in-memory copy (tests repoint SETTINGS_FILE between cases)."""
    with _settings_lock:
        _settings["obj"] = None


# ── autostart entry (§14 POST /api/settings autostart_enabled) ──────────────

_AUTOSTART_KEY = "X-GNOME-Autostart-enabled="


def read_autostart():
    """The entry's X-GNOME-Autostart-enabled value; None when the entry is not installed."""
    text = read_file(AUTOSTART_FILE)
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith(_AUTOSTART_KEY):
            return line[len(_AUTOSTART_KEY):].strip().lower() == "true"
    return True        # the key is optional; absent means enabled


def set_autostart(enabled):
    """Rewrite only the X-GNOME-Autostart-enabled= line of an existing entry. (ok, error)."""
    path = AUTOSTART_FILE
    try:
        with open(path, encoding="utf-8", newline="") as f:
            lines = f.read().splitlines(keepends=True)
        mode = os.stat(path).st_mode & 0o7777
    except OSError:
        return False, "The autostart entry is not installed (run install.sh to create it)."
    new_line = f"{_AUTOSTART_KEY}{'true' if enabled else 'false'}\n"
    out, done = [], False
    for line in lines:
        if not done and line.startswith(_AUTOSTART_KEY):
            out.append(new_line)
            done = True
        else:
            out.append(line)
    if not done:
        # Keep it inside [Desktop Entry] (install.sh always writes the line,
        # so this only repairs a hand-edited file).
        at = next((i + 1 for i, l in enumerate(out) if l.strip() == "[Desktop Entry]"), len(out))
        if out and at == len(out) and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.insert(at, new_line)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write("".join(out))
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False, f"Could not update the autostart entry: {e.strerror or e}."
    return True, None


# ─────────────────────────────────────────────────────────────────────────────
# Session flags (never persisted)
# ─────────────────────────────────────────────────────────────────────────────

_rt_lock = threading.Lock()
_RT = {
    "vrm_desired": False,              # §15 session intent
    "tdp_locked": False,               # effective lock (§9: persisted value honoured only after a startup apply)
    "tdp_lock_paused_thermal": False,
    "startup_tdp_skipped": None,       # "unclean_exit" | "above_25w" | None
    "hold_owned": False,               # this process created the daemon's current override
    "owned_set_at": None,              # that override's set_at as the daemon reported it
    "owned_wall": 0.0,                 # time.time() when the hold was accepted
    "last_resume_mono": -1e9,
    "workers_started": False,
    "released": False,
}


def _rt(key):
    with _rt_lock:
        return _RT[key]


def _rt_set(**kw):
    with _rt_lock:
        _RT.update(kw)


def last_resume_age():
    """Seconds since this process last asked for a resume (app.py: 'did a hold end by itself?')."""
    return time.monotonic() - _rt("last_resume_mono")


def _owns_override(state):
    """
    True when the daemon's current override is the one this process created.

    Matched on the daemon's own set_at, so a later hold by another client is
    never resumed by our quit. A state.json published before our hold was
    accepted cannot disprove ownership, so in that window our record wins.
    """
    with _rt_lock:
        owned, set_at, wall = _RT["hold_owned"], _RT["owned_set_at"], _RT["owned_wall"]
    if not owned or not isinstance(state, dict):
        return False
    ts = _num(state.get("ts"))
    if ts is not None and ts <= wall:
        return True
    ov = state.get("override")
    if not isinstance(ov, dict):
        return False
    theirs = _num(ov.get("set_at"))
    return set_at is None or (theirs is not None and abs(theirs - set_at) < 0.01)


# ─────────────────────────────────────────────────────────────────────────────
# state.json / history.json (§5, §6) and the liveness rule
# ─────────────────────────────────────────────────────────────────────────────

def _read_json_obj(path):
    text = read_file(path)
    if not text:
        return None
    try:
        obj = _loads_lenient(text)
    except (ValueError, RecursionError):          # JSONDecodeError is a ValueError
        return None
    return obj if isinstance(obj, dict) else None


def read_state():
    """The daemon's state.json, or None when absent, unparseable or without a numeric ts."""
    st = _read_json_obj(state_file())
    return st if st is not None and _num(st.get("ts")) is not None else None


def read_history():
    return _read_json_obj(history_file())


def state_age(state):
    ts = _num(state.get("ts")) if isinstance(state, dict) else None
    return None if ts is None else time.time() - ts


def _sample_interval(state):
    si = _num(state.get("sample_interval")) if isinstance(state, dict) else None
    return si if si is not None and si > 0 else 1


_UNSET = object()


def daemon_active(state=_UNSET):
    """§5: state.json exists, parses, and time.time() - ts < 3*sample_interval + 2.

    A ts further in the *future* than that window is treated as stale too:
    it only happens when the wall clock stepped back after the daemon's last
    write, and a live daemon republishes (with a sane ts) within one tick,
    whereas a dead one would otherwise look alive until the clock caught up.
    """
    if state is _UNSET:
        state = read_state()
    age = state_age(state)
    limit = 3 * _sample_interval(state) + 2
    return age is not None and -limit < age < limit


def control_temp_now(state=_UNSET):
    """state.temp_raw while the daemon is active, else max(cpu, igpu) as floats; None if unknown."""
    if state is _UNSET:
        state = read_state()
    if daemon_active(state):
        raw = _num(state.get("temp_raw"))
        if raw is not None:
            return float(raw)
    return read_control_temp_raw()


# ─────────────────────────────────────────────────────────────────────────────
# Daemon config (read-only here; the daemon's sanitize() is the validator)
# ─────────────────────────────────────────────────────────────────────────────

_cfg_lock = threading.Lock()
_cfg_cache = {"key": None, "cfg": None}


def config_mtime():
    try:
        return os.stat(CONFIG_FILE).st_mtime
    except OSError:
        return None


def load_config():
    """config.json as on disk, with missing keys filled from DEFAULT_CONFIG for display."""
    path = CONFIG_FILE
    with _cfg_lock:
        try:
            key = (path, os.stat(path).st_mtime_ns)
        except OSError:
            key = (path, None)
        if key[1] is not None and key == _cfg_cache["key"]:
            return copy.deepcopy(_cfg_cache["cfg"])
        cfg, raw = copy.deepcopy(DEFAULT_CONFIG), {}
        text = read_file(path) if key[1] is not None else None
        if text:
            try:
                parsed = _loads_lenient(text)
                if isinstance(parsed, dict):
                    raw = parsed
            except (ValueError, RecursionError):
                _log_once("cfg-parse", f"config.json is not valid JSON ({path})")
        raw.pop("poll_interval", None)             # v1 key, ignored by v2 (§3)
        cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
        if "override_ceiling_temp" not in raw and _is_int(cfg.get("critical_temp")):
            cfg["override_ceiling_temp"] = cfg["critical_temp"] - 8      # §3 default
        _cfg_cache["key"], _cfg_cache["cfg"] = key, cfg
        return copy.deepcopy(cfg)


def _cfg_int(key, default, lo, hi):
    v = load_config().get(key)
    return v if _is_int(v) and lo <= v <= hi else default


def critical_temp_now(state=_UNSET):
    """critical_temp from a live state.json, else from config.json (default 90)."""
    if state is _UNSET:
        state = read_state()
    if daemon_active(state):
        v = state.get("critical_temp")
        if _is_int(v) and 70 <= v <= 105:
            return v
    return _cfg_int("critical_temp", 90, 70, 105)


# ─────────────────────────────────────────────────────────────────────────────
# Control socket client (§7)
# ─────────────────────────────────────────────────────────────────────────────

_MUTATING_CMDS = frozenset({"hold", "resume", "test_alert"})


def daemon_request(obj, timeout=SOCKET_TIMEOUT_S):
    """
    One request/reply on the control socket.

    Returns the reply dict, or None when no daemon is listening (no socket
    file, or connection refused). A daemon that is there but does not
    answer yields {"ok": False, ...}: callers must NOT fall back to direct
    fan writes then, because a stalled daemon wakes up and fights them.

    Dry run never actuates the production daemon; a socket elsewhere
    (FANCTL_RUNTIME_DIR, i.e. a test double) is talked to for real.
    """
    path = control_sock()
    if DRY_RUN and obj.get("cmd") in _MUTATING_CMDS and _runtime_is_production():
        if not os.path.exists(path):
            return None
        _log(f"dry-run: socket {obj.get('cmd')} skipped")
        return {"ok": True, "state": read_state(), "dry_run": True}

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        try:
            s.connect(path)
        except (FileNotFoundError, ConnectionRefusedError):
            return None
        except PermissionError:
            return {"ok": False, "error": "permission",
                    "message": "No permission to open the daemon's control socket "
                               "(is FANCTL_GUI_GID in daemon.env one of your groups?)."}
        s.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf and len(buf) < 1 << 20:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        return {"ok": False, "error": "timeout",
                "message": f"The daemon did not answer within {timeout:g} s."}
    except OSError as e:
        return {"ok": False, "error": "socket", "message": f"Control socket error: {e.strerror or e}."}
    finally:
        s.close()
    line = buf.split(b"\n", 1)[0].strip()
    if not line:
        # §7: the daemon closes silently on a peer uid it does not accept.
        return {"ok": False, "error": "empty_reply",
                "message": "The daemon closed the connection without replying "
                           "(is FANCTL_GUI_UID in daemon.env your user id?)."}
    try:
        reply = _loads_lenient(line.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        reply = None
    if not isinstance(reply, dict):
        return {"ok": False, "error": "bad_reply", "message": "The daemon sent an unreadable reply."}
    return reply


def _daemon_error_text(reply):
    msg = reply.get("message") if isinstance(reply, dict) else None
    if isinstance(msg, str) and msg.strip():
        return msg.strip()
    code = reply.get("error") if isinstance(reply, dict) else None
    return f"The daemon rejected the request ({code})." if code else "The daemon rejected the request."


# ─────────────────────────────────────────────────────────────────────────────
# SMU readings (ryzenadj --info), cached
#
# --info reads the PM table through /dev/mem on a machine with a GPU-hang
# history, so the cadence is slow (10 s with the window visible, 30 s in the
# tray) and a failure keeps the last good numbers flagged stale. A value is
# never invented: v1 turned a failed read into a "22 W" that the lock loop
# then wrote to the hardware.
# ─────────────────────────────────────────────────────────────────────────────

_SMU_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|")
_SMU_KEYS = ("tdp", "power", "power_slow", "edc", "edc_limit", "tdc", "tdc_limit", "thm_limit")
_smu_lock = threading.Lock()          # one ryzenadj process at a time, read or write


def parse_ryzenadj_info(returncode, stdout, stderr=""):
    """One `ryzenadj --info` run → the SMU dict; raises RuntimeError on any failure."""
    if returncode != 0:
        lines = [l for l in (stderr or "").strip().splitlines() if l.strip()]
        raise RuntimeError(f"ryzenadj --info failed: {lines[-1] if lines else f'exit {returncode}'}")
    vals = {}
    for line in (stdout or "").splitlines():
        m = _SMU_ROW.match(line)
        if not m:
            continue
        try:
            v = float(m.group(2))
        except ValueError:
            continue                        # the header row
        if v == v and v not in (float("inf"), float("-inf")):
            vals[m.group(1)] = v            # "nan" rows are dropped → null
    if "STAPM LIMIT" not in vals:
        raise RuntimeError("ryzenadj --info printed no STAPM LIMIT row")

    def g(key):
        v = vals.get(key)
        return None if v is None else round(v, 1)

    return {
        # An int keeps tdp comparable with tdp_requested without float noise.
        "tdp":        int(round(vals["STAPM LIMIT"])),
        "power":      g("PPT VALUE FAST"),
        "power_slow": g("PPT VALUE SLOW"),
        "edc":        g("EDC VALUE VDD"),
        "edc_limit":  g("EDC LIMIT VDD"),
        "tdc":        g("TDC VALUE VDD"),
        "tdc_limit":  g("TDC LIMIT VDD"),
        "thm_limit":  g("THM LIMIT CORE"),
    }


def _ryzenadj_info():
    if DRY_RUN:
        # Dry run never forks sudo, not even to read: the SMU reads as unavailable.
        raise RuntimeError("dry run: ryzenadj --info not executed")
    with _smu_lock:
        r = subprocess.run(["sudo", "-n", RYZENADJ, "--info"],
                           capture_output=True, text=True, timeout=8)
    return parse_ryzenadj_info(r.returncode, r.stdout, r.stderr)


class SmuCache:
    def __init__(self, reader=None):
        # `reader` is injectable so tests exercise ok/stale handling without sudo.
        self.reader = reader
        self.value = None              # last good reading
        self.ok = False                # the most recent attempt succeeded
        self.error = None
        self.updated_at = None         # epoch of the last good reading
        self.visible = True
        self._attempt_mono = -1e9
        self._lock = threading.Lock()

    def ttl(self):
        return SMU_TTL_VISIBLE if self.visible else SMU_TTL_HIDDEN

    def get(self, force=False):
        """Refresh when the TTL elapsed (or forced) and return a snapshot."""
        if force or time.monotonic() - self._attempt_mono >= self.ttl():
            # A status poll never queues behind a slow ryzenadj: if another
            # thread is refreshing right now it gets the current snapshot.
            if self._lock.acquire(blocking=bool(force)):
                try:
                    now = time.monotonic()
                    if force or now - self._attempt_mono >= self.ttl():
                        self._attempt_mono = now
                        try:
                            value = (self.reader or _ryzenadj_info)()
                            self.value, self.ok, self.error = value, True, None
                            self.updated_at = time.time()
                        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
                            self.ok, self.error = False, str(e)
                            _log_once("smu", f"SMU read failed: {e}")
                finally:
                    self._lock.release()
        return self.snapshot()

    def snapshot(self):
        """§15 smu object from what is cached; never triggers a read."""
        v = self.value or {}
        out = {"ok": self.ok, "stale": (not self.ok) and self.value is not None}
        for k in _SMU_KEYS:
            out[k] = v.get(k)
        out["updated_at"] = self.updated_at
        out["error"] = None if self.ok else self.error
        return out


SMU = SmuCache()


def set_visibility(visible):
    """app.py: window shown → 10 s SMU cadence; hidden / tray only → 30 s."""
    SMU.visible = bool(visible)


def vrm_unlocked_hw(smu=None):
    """§15 hardware truth: the SMU's EDC limit is the unlocked 60 A rather than the stock 45 A."""
    edc = _num((smu if smu is not None else SMU.snapshot()).get("edc_limit"))
    return edc is not None and edc >= VRM_UNLOCKED_EDC_A


# ─────────────────────────────────────────────────────────────────────────────
# Root wrappers (sudo -n). Dry run is checked by every caller AND refused
# here, so a forgotten check fails loudly instead of touching hardware.
# ─────────────────────────────────────────────────────────────────────────────

def _sudo(argv, timeout, stdin=None):
    if DRY_RUN:
        return subprocess.CompletedProcess(argv, 125, "", "dry run: sudo refused")
    try:
        return subprocess.run(["sudo", "-n"] + argv, input=stdin,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"{os.path.basename(argv[0])} timed out after {timeout} s")
    except OSError as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def _stderr_tail(r, default):
    lines = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
    return lines[-1] if lines else default


def _fan_set_direct(level, watchdog):
    """`fan-set-level.sh <level> <watchdog>`: the wrapper writes the watchdog first. (ok, error)."""
    level = str(level)
    if level not in VALID_LEVELS or not _is_int(watchdog) or not 0 <= watchdog <= 120:
        return False, "Invalid fan level or watchdog value."
    if DRY_RUN:
        _log(f"dry-run: fan-set-level.sh {level} {watchdog} skipped")
        return True, None
    if not _runtime_is_production():
        # A test runtime dir makes a live daemon look absent; a real write
        # would then fight it. Direct writes need the real liveness signal.
        return False, "Refusing a direct fan write with a non-default FANCTL_RUNTIME_DIR."
    r = _sudo([FAN_SET, level, str(watchdog)], timeout=8)
    if r.returncode != 0:
        return False, _stderr_tail(r, f"fan-set-level.sh exited {r.returncode}")
    return True, None


def apply_tdp(watts, vrm, reason):
    """Run ryzenadj-set-tdp.sh after checking the daemon.env caps. (ok, error)."""
    caps = tdp_caps()
    if not _is_int(watts):
        return False, "TDP must be a whole number of watts."
    if not TDP_ABS_MIN <= watts <= caps["tdp_max"]:
        return False, f"TDP must be between {TDP_ABS_MIN} and {caps['tdp_max']} W."
    if vrm and watts > caps["tdp_max_vrm_unlocked"]:
        return False, (f"With the VRM current limits unlocked the TDP is capped at "
                       f"{caps['tdp_max_vrm_unlocked']} W.")
    vrm_arg = "1" if vrm else "0"
    if DRY_RUN:
        _log(f"dry-run: ryzenadj-set-tdp.sh {watts} {vrm_arg} skipped ({reason})")
        return True, None
    with _smu_lock:
        r = _sudo([TDP_SET, str(watts), vrm_arg], timeout=10)
    if r.returncode != 0:
        err = _stderr_tail(r, f"ryzenadj-set-tdp.sh exited {r.returncode}")
        _log(f"TDP {watts} W vrm={vrm_arg} FAILED ({reason}): {err}")
        return False, err
    _log(f"TDP applied {watts} W vrm={vrm_arg} ({reason})")
    SMU.get(force=True)          # the UI must not lag a value we just caused
    return True, None


def daemon_ctrl(action):
    if action not in ("start", "stop", "restart"):
        return {"success": False, "error": "Unknown daemon action."}
    if DRY_RUN:
        _log(f"dry-run: systemctl {action} skipped")
        return {"success": True, "error": None}
    r = _sudo([SYSTEMCTL, action, SERVICE], timeout=30)
    if r.returncode != 0:
        return {"success": False, "error": _stderr_tail(r, f"systemctl {action} exited {r.returncode}")}
    if action != "start":
        # stop returns the fan to firmware; restart drops the in-memory
        # override. Either way nothing of ours is held any more.
        _rt_set(hold_owned=False, owned_set_at=None)
    return {"success": True, "error": None}


class _Cached:
    """TTL cache for a cheap unprivileged fork (systemctl is-enabled)."""

    def __init__(self, fn, ttl):
        self.fn, self.ttl = fn, ttl
        self.value, self.at = None, -1e9
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            now = time.monotonic()
            if now - self.at >= self.ttl:
                self.at = now
                try:
                    self.value = self.fn()
                except (OSError, subprocess.SubprocessError) as e:
                    self.value = None
                    _log_once("cached-" + self.fn.__name__, f"{self.fn.__name__}: {e}")
            return self.value


def _daemon_enabled():
    if DRY_RUN:
        return None
    r = subprocess.run([SYSTEMCTL, "is-enabled", SERVICE], capture_output=True, text=True, timeout=5)
    word = r.stdout.strip()
    return None if not word else word == "enabled"


DAEMON_ENABLED = _Cached(_daemon_enabled, 60.0)


# ─────────────────────────────────────────────────────────────────────────────
# Daemon-less hold: EC watchdog + keep-alive with the critical fallback (§14)
#
# The wrapper arms `watchdog 120` before each level write, so a GUI that dies
# without its release path (SIGKILL, OOM, X going away) hands the fan back
# to firmware auto within two minutes. While we live the level is re-written
# every 30 s and the temperature checked every 2 s, so a low hold cannot ride
# through critical_temp.
# ─────────────────────────────────────────────────────────────────────────────

class ManualFallback:

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.level = None
        self.until_mono = None
        self.manual_critical = False
        self.last_end = None           # {"reason", "level", "ts"} of the last hold that ended by itself
        self._reset_episode()

    def _reset_episode(self):
        self._critical_since = None
        self._cool_since = None
        self._misses = 0
        self._last_write = None
        self._written_level = None

    @property
    def active(self):
        t = self._thread
        return t is not None and t.is_alive()

    def remaining_s(self):
        until = self.until_mono
        if not self.active or until is None:
            return None
        return max(0, int(until - time.monotonic()))

    def start(self, level, seconds):
        """Supervise `level` for `seconds`. The caller wrote it already (and stopped any old hold first)."""
        self.stop()
        with self._lock:
            now = time.monotonic()
            self._reset_episode()
            self.level, self.until_mono, self.manual_critical = level, now + seconds, False
            self._last_write, self._written_level = now, level
            # A fresh Event per thread: re-using one could revive a stopping thread.
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,),
                                            name="fan-keepalive", daemon=True)
            self._thread.start()

    def stop(self):
        """Stop supervising without touching the fan (the caller writes `auto 0` when needed)."""
        with self._lock:
            t = self._thread
            self._stop.set()
        if t is not None and t is not threading.current_thread():
            t.join(timeout=10)         # at most one in-flight sudo write (8 s timeout)
        with self._lock:
            if self._thread is t:
                self._thread = None
            self.manual_critical = False
            self.until_mono = None

    # ── supervision ──────────────────────────────────────────────────────────

    def _write(self, level, watchdog, stop_event, now):
        if stop_event.is_set():
            return False               # a resume/quit owns the fan now
        ok, err = _fan_set_direct(level, watchdog)
        if ok:
            self._last_write, self._written_level = now, level
        else:
            _log_once(f"keepalive-{level}", f"keep-alive: fan-set-level.sh {level} {watchdog} failed: {err}")
        return ok

    def _due(self, level, now):
        return (self._written_level != level or self._last_write is None
                or now - self._last_write >= FALLBACK_REFRESH_S)

    def tick(self, now, stop_event):
        """One supervision step: None to continue, or why the hold ended."""
        if daemon_active():
            # The daemon writes its own watchdog and level on start.
            _log("keep-alive: the daemon is back and owns the fan again")
            return "daemon"
        with self._lock:
            level, until = self.level, self.until_mono
        temp = read_control_temp_raw()
        if temp is None:
            self._misses += 1
            if self._misses >= 3:
                # Same rule as the daemon: three misses and firmware is the
                # only safe owner, because nobody here can judge the heat.
                _log("keep-alive: no readable temperature sensor, fan back to firmware auto")
                self._write("auto", 0, stop_event, now)
                return "sensor_lost"
            want = "disengaged" if self.manual_critical else level
            if self._due(want, now):
                self._write(want, FALLBACK_WATCHDOG, stop_event, now)
            return None
        self._misses = 0
        crit = critical_temp_now(None)

        if not self.manual_critical:
            if temp >= crit:
                # One sample suffices (the daemon wants two): this is the
                # degraded path and the error direction is a louder fan.
                self.manual_critical, self._critical_since, self._cool_since = True, now, None
                _log(f"keep-alive: {temp:.1f} °C >= critical {crit} °C, fan forced to disengaged")
                self._write("disengaged", FALLBACK_WATCHDOG, stop_event, now)
                return None
            if until is not None and now >= until:
                _log(f"keep-alive: hold {level} expired, fan back to firmware auto")
                self._write("auto", 0, stop_event, now)
                return "expired"
            if self._due(level, now):
                self._write(level, FALLBACK_WATCHDOG, stop_event, now)
            return None

        # Critical episode: disengaged until clearly cooler for a while and at
        # least the daemon's exit hold since entry. Expiry does not end it
        # early: firmware auto at 90 °C would drop the fan from 6400 RPM.
        if temp < crit - _cfg_int("critical_exit_margin", 8, 3, 20):
            if self._cool_since is None:
                self._cool_since = now
        else:
            self._cool_since = None
        hold_s = _cfg_int("critical_exit_hold_s", 60, 10, 600)
        if (self._cool_since is not None and now - self._cool_since >= FALLBACK_COOL_CONFIRM_S
                and now - self._critical_since >= hold_s):
            # Not back to the held level: it just proved unable to keep the
            # die under critical_temp, and returning to it would start a
            # level <-> disengaged cycle. Firmware auto owns the fan from here.
            _log(f"keep-alive: cooled to {temp:.1f} °C after critical, fan back to firmware auto "
                 f"(hold {level} ended)")
            self._write("auto", 0, stop_event, now)
            return "critical_cleared"
        if self._due("disengaged", now):
            self._write("disengaged", FALLBACK_WATCHDOG, stop_event, now)
        return None

    def _run(self, stop_event):
        reason = None
        try:
            while not stop_event.wait(FALLBACK_TICK_S):
                reason = self.tick(time.monotonic(), stop_event)
                if reason:
                    break
        except Exception as e:                  # noqa: BLE001 — never leave a low level unwatched
            _log(f"keep-alive crashed ({e!r}); fan back to firmware auto")
            if not stop_event.is_set():
                _fan_set_direct("auto", 0)
            reason = "error"
        finally:
            with self._lock:
                if reason:
                    self.last_end = {"reason": reason, "level": self.level, "ts": time.time()}
                if self._thread is threading.current_thread():
                    self._thread = None
                    self.manual_critical = False
                    self.until_mono = None


FALLBACK = ManualFallback()


# ─────────────────────────────────────────────────────────────────────────────
# Fan actions (§14 /api/fan/*)
# ─────────────────────────────────────────────────────────────────────────────

# Serialises hold/resume so a resume's "auto" write and a concurrent hold's
# keep-alive start cannot interleave.
_fan_lock = threading.RLock()


def _err(msg, **extra):
    return {"success": False, "error": msg, **extra}


def _parse_level(level):
    """A §3 level string, or None. JSON ints 0-7 are accepted as their string form."""
    if _is_int(level) and 0 <= level <= 7:
        level = str(level)
    return level if isinstance(level, str) and level in VALID_LEVELS else None


def _parse_int(v, digits=6):
    """A whole number from a JSON int or a plain digit string (no sign, no leading
    zeros); None for bool/float/garbage."""
    if _is_int(v):
        return v
    if isinstance(v, str) and re.fullmatch(r"0|[1-9]\d{0,%d}" % (digits - 1), v.strip()):
        return int(v.strip())
    return None


def _state_of(reply):
    st = reply.get("state") if isinstance(reply, dict) else None
    return st if isinstance(st, dict) else None


def fan_hold(level, seconds=None):
    """Hold the fan at `level` for `seconds` (0 = the configured maximum). Socket first, fallback second."""
    lvl = _parse_level(level)
    if lvl is None:
        return _err("Unknown fan level; use 0-7, auto, disengaged or full-speed.")
    if seconds is None:
        seconds = get_settings()["ui"]["hold_default_seconds"]
    secs = _parse_int(seconds)
    if secs is None or secs < 0:
        return _err("Hold duration must be a whole number of seconds (0 = until resumed).")
    secs = min(secs, OVERRIDE_HARD_MAX_S)        # the daemon clamps further to override_max_seconds

    with _fan_lock:
        reply = daemon_request({"cmd": "hold", "level": lvl, "seconds": secs})
        if reply is not None:
            if FALLBACK.active:
                FALLBACK.stop()                  # the daemon owns the fan again
            if reply.get("ok"):
                st = _state_of(reply)
                ov = st.get("override") if st else None
                _rt_set(hold_owned=True, owned_wall=time.time(),
                        owned_set_at=_num(ov.get("set_at")) if isinstance(ov, dict) else None)
                return {"success": True, "error": None, "fallback": False, "state": st}
            return _err(_daemon_error_text(reply), fallback=False, code=reply.get("error"))
        if daemon_active():
            return _err("The daemon is running but its control socket is unreachable; "
                        "refusing to write the fan behind its back.", fallback=False, code="socket_unreachable")

        # ── daemon down: supervised direct write ─────────────────────────────
        if lvl == "auto":
            return _resume_without_daemon()      # "hold at firmware auto" is firmware auto
        fp = read_fan_proc()
        if not fp["control_available"]:
            # Never judged by "status: disabled", which only means level 0.
            return _err("Fan control is not available: /proc/acpi/ibm/fan offers no 'level' command "
                        "(thinkpad_acpi must be loaded with fan_control=1).",
                        fallback=True, code="fan_control_unavailable")
        temp = read_control_temp_raw()
        if lvl == "0" and (temp is None or temp >= FAN_OFF_MAX_TEMP):
            now_txt = f"now {temp:.0f} °C" if temp is not None else "temperature unreadable"
            return _err(f"Fan off is only allowed below {FAN_OFF_MAX_TEMP} °C ({now_txt}).",
                        fallback=True, code="too_hot_for_fan_off")
        crit = critical_temp_now(None)
        if temp is not None and temp >= crit and LEVEL_RANK[lvl] < LEVEL_RANK["disengaged"]:
            return _err(f"{temp:.0f} °C is at or above the {crit} °C critical limit; without the "
                        "daemon only Maximum can be held right now.", fallback=True, code="critical")
        cap = _cfg_int("override_max_seconds", OVERRIDE_DEFAULT_S, 60, OVERRIDE_HARD_MAX_S)
        hold_s = cap if secs == 0 else min(secs, cap)
        # Stop the old supervision BEFORE writing, or it could re-write the
        # previous level after ours.
        FALLBACK.stop()
        ok, err = _fan_set_direct(lvl, FALLBACK_WATCHDOG)
        if not ok:
            return _err(f"Could not set the fan level: {err}", fallback=True)
        FALLBACK.start(lvl, hold_s)
        _rt_set(hold_owned=False, owned_set_at=None)
        _log(f"hold {lvl} without the daemon (EC watchdog {FALLBACK_WATCHDOG} s, {fmt_duration(hold_s)})")
        return {"success": True, "error": None, "fallback": True, "seconds": hold_s}


def _resume_without_daemon():
    FALLBACK.stop()                          # first, so it cannot re-write the level
    ok, err = _fan_set_direct("auto", 0)
    if not ok:
        return _err(f"Could not return the fan to firmware control: {err}", fallback=True)
    _rt_set(hold_owned=False, owned_set_at=None)
    return {"success": True, "error": None, "fallback": True}


def fan_resume():
    """Clear any hold: socket `resume`, or stop the keep-alive and write `auto 0`."""
    with _fan_lock:
        _rt_set(last_resume_mono=time.monotonic())
        reply = daemon_request({"cmd": "resume"})
        if reply is not None:
            if FALLBACK.active:
                FALLBACK.stop()
            if reply.get("ok"):
                _rt_set(hold_owned=False, owned_set_at=None)
                return {"success": True, "error": None, "fallback": False, "state": _state_of(reply)}
            return _err(_daemon_error_text(reply), fallback=False, code=reply.get("error"))
        if daemon_active():
            return _err("The daemon is running but its control socket is unreachable.",
                        fallback=False, code="socket_unreachable")
        return _resume_without_daemon()


# ─────────────────────────────────────────────────────────────────────────────
# TDP actions (§14 /api/tdp/*)
# ─────────────────────────────────────────────────────────────────────────────

# One TDP decision at a time: a set/unlock/restore from the page and a lock
# re-apply must never interleave (the loop could re-apply the old request
# right after the page applied a new one).
_tdp_op_lock = threading.RLock()


def _smu_for_action():
    """A good SMU reading for a user-initiated action: the cached one, else one forced read."""
    smu = SMU.get()
    return smu if smu["ok"] else SMU.get(force=True)


def _current_watts():
    """tdp_requested, else the SMU value when (and only when) the reading is good."""
    req = get_settings()["tdp_requested"]
    if req is not None:
        return req, None
    smu = _smu_for_action()
    if not smu["ok"] or smu["tdp"] is None:
        return None, "The SMU reading is unavailable, so the current TDP is unknown; apply a TDP first."
    return smu["tdp"], None


def set_tdp(watts):
    w = _parse_int(watts, digits=3)
    if w is None:
        return _err("TDP must be a whole number of watts.")
    with _tdp_op_lock:
        ok, err = apply_tdp(w, _rt("vrm_desired"), "set")
        if not ok:
            return _err(err)
        update_settings({"tdp_requested": w})
    return {"success": True, "error": None, "tdp_requested": w}


def set_tdp_lock(locked):
    if not isinstance(locked, bool):
        return _err("'locked' must be true or false.")
    with _tdp_op_lock:
        if locked and get_settings()["tdp_requested"] is None:
            smu = _smu_for_action()
            if not smu["ok"] or smu["tdp"] is None:
                return _err("Cannot lock: no TDP has been requested and the SMU reading is "
                            "unavailable. Apply a TDP first.")
            caps = tdp_caps()
            if not TDP_ABS_MIN <= smu["tdp"] <= caps["tdp_max"]:
                return _err(f"Cannot lock: the SMU reports {smu['tdp']} W, outside "
                            f"{TDP_ABS_MIN}-{caps['tdp_max']} W. Apply a TDP first.")
            update_settings({"tdp_requested": smu["tdp"]})
        s = update_settings({"tdp_locked": locked})
        _rt_set(tdp_locked=locked)
        if not locked:
            _rt_set(tdp_lock_paused_thermal=False)
    return {"success": True, "error": None, "tdp_locked": locked, "tdp_requested": s["tdp_requested"]}


def set_vrm_unlock(unlocked):
    if not isinstance(unlocked, bool):
        return _err("'unlocked' must be true or false.")
    with _tdp_op_lock:
        caps = tdp_caps()
        watts, why = _current_watts()
        if watts is None:
            if unlocked:
                return _err(why)
            # Going back to stock must never be blocked by an unknown TDP
            # (SMU unreadable, nothing requested): use restore_stock's value.
            watts = TDP_RESTORE_DEFAULT
        if unlocked and watts > caps["tdp_max_vrm_unlocked"]:
            return _err(f"Lower the TDP to {caps['tdp_max_vrm_unlocked']} W or below before unlocking")
        if not unlocked:
            # Re-locking must not fail because daemon.env was lowered after
            # the request; stock intent holds even if the apply fails.
            watts = max(TDP_ABS_MIN, min(watts, caps["tdp_max"]))
            _rt_set(vrm_desired=False)
        ok, err = apply_tdp(watts, unlocked, "vrm unlock" if unlocked else "vrm stock")
        if not ok:
            return _err(err)
        _rt_set(vrm_desired=unlocked)            # an unlock counts only once the hardware took it
    return {"success": True, "error": None, "vrm_desired": unlocked, "tdp": watts}


def restore_stock():
    """`tdp_requested or 15` W with stock VRM; the session intent becomes stock."""
    with _tdp_op_lock:
        caps = tdp_caps()
        req = get_settings()["tdp_requested"]
        watts = max(TDP_ABS_MIN, min(req if req is not None else TDP_RESTORE_DEFAULT, caps["tdp_max"]))
        _rt_set(vrm_desired=False)
        ok, err = apply_tdp(watts, False, "restore stock")
    if not ok:
        return _err(err)
    return {"success": True, "error": None, "tdp": watts, "vrm_desired": False}


class TdpLock:
    """
    §14 lock decisions, free of I/O so the tests can drive them.

    Re-applies the remembered *request*, never what the SMU reports (v1 did,
    which locked in a firmware reset), and only when something says it is
    needed. Events are latched until a re-apply succeeds: a single tick can
    be blocked by the 20 s floor, a thermal pause or a failed SMU read, and a
    resume nobody acted on is exactly the case the lock exists for.
    """

    def __init__(self):
        self.last_wall = None
        self.last_mono = None
        self.last_ac = None
        self.last_apply_mono = -1e9
        self.pending = set()

    def observe(self, now_wall, now_mono, on_ac):
        if self.last_wall is not None:
            # CLOCK_MONOTONIC stops during suspend while the wall clock runs on.
            if (now_wall - self.last_wall) - (now_mono - self.last_mono) > SUSPEND_GAP_S:
                self.pending.add("suspend/resume")
            if on_ac != self.last_ac:
                self.pending.add("power source changed")
        self.last_wall, self.last_mono, self.last_ac = now_wall, now_mono, on_ac

    def decide(self, now_mono, locked, requested, vrm_want, smu, temp, crit, caps):
        """(action, reasons, paused); action ∈ idle|wait_smu|ok|paused|floor|over_cap|apply."""
        if not locked or requested is None:
            self.pending.clear()
            return "idle", [], False
        # Unknown temperature counts as hot: never push power blind.
        paused = temp is None or temp >= crit - TDP_THERMAL_MARGIN
        if not smu.get("ok") or smu.get("tdp") is None:
            return "wait_smu", [], paused        # never act on stale numbers
        reasons = []
        if abs(smu["tdp"] - requested) >= 1:
            reasons.append(f"drift {smu['tdp']}->{requested} W")
        if _num(smu.get("edc_limit")) is not None and vrm_unlocked_hw(smu) != vrm_want:
            reasons.append("vrm " + ("unlocked" if vrm_want else "stock") + " expected")
        reasons += sorted(self.pending)
        if not reasons:
            return "ok", [], paused
        if paused:
            return "paused", reasons, paused
        if now_mono - self.last_apply_mono < TDP_LOCK_FLOOR_S:
            return "floor", reasons, paused
        if requested > caps["tdp_max"] or (vrm_want and requested > caps["tdp_max_vrm_unlocked"]):
            return "over_cap", reasons, paused
        self.last_apply_mono = now_mono          # failures count too: no hammering
        return "apply", reasons, paused

    def applied(self, ok):
        if ok:
            self.pending.clear()


TDP_LOCK = TdpLock()


def tdp_lock_tick(stop_event=None):
    """One 5 s lock-loop iteration; returns the decision's action word."""
    TDP_LOCK.observe(time.time(), time.monotonic(), read_power()["on_ac"])
    with _tdp_op_lock:
        if stop_event is not None and stop_event.is_set():
            return "stopped"                     # quitting: release_all owns the SMU now
        locked, vrm_want = _rt("tdp_locked"), _rt("vrm_desired")
        requested = get_settings()["tdp_requested"]
        if not (locked and requested is not None):
            TDP_LOCK.decide(time.monotonic(), False, None, vrm_want, {}, None, 0, {})
            _rt_set(tdp_lock_paused_thermal=False)
            return "idle"                        # no SMU read unless the lock needs one
        state = read_state()
        temp, crit = control_temp_now(state), critical_temp_now(state)
        action, reasons, paused = TDP_LOCK.decide(time.monotonic(), True, requested, vrm_want,
                                                  SMU.get(), temp, crit, tdp_caps())
        _rt_set(tdp_lock_paused_thermal=paused)
        if action == "paused":
            t = "unknown" if temp is None else f"{temp:.0f} °C"
            _log_once("lock-paused", f"TDP lock paused (thermal, {t}): " + ", ".join(reasons))
        elif action == "over_cap":
            _log_once("lock-cap", f"TDP lock: {requested} W exceeds the daemon.env cap; not re-applying")
        elif action == "apply":
            ok, err = apply_tdp(requested, vrm_want, "lock: " + ", ".join(reasons))
            TDP_LOCK.applied(ok)
            if not ok:
                _log_once("lock-fail", f"TDP lock re-apply failed: {err}")
        return action


def tdp_lock_loop(stop_event, startup=None):
    """Worker thread: the §9 startup rule (off the GTK thread: sudo takes seconds), then the lock."""
    if startup is not None:
        try:
            _apply_startup_tdp(*startup)
        except Exception as e:                   # noqa: BLE001 — the lock loop must still run
            _log(f"startup TDP rule failed: {e!r}")
    TDP_LOCK.observe(time.time(), time.monotonic(), read_power()["on_ac"])
    while not stop_event.wait(TDP_LOCK_TICK_S):
        try:
            tdp_lock_tick(stop_event)
        except Exception as e:                   # noqa: BLE001 — one bad tick must not end the lock
            _log_once("lock-crash", f"TDP lock tick failed: {e!r}")


def _apply_startup_tdp(s, clean):
    """§9: re-apply a remembered TDP only when opted in, after a clean exit, and ≤ 25 W."""
    req = s["tdp_requested"]
    if not s["apply_tdp_at_startup"] or req is None:
        _rt_set(startup_tdp_skipped=None)
        return "not_requested"
    if not clean:
        _rt_set(startup_tdp_skipped="unclean_exit")
        _log("startup TDP not applied: the previous session did not exit cleanly")
        return "unclean_exit"
    if not TDP_ABS_MIN <= req <= STARTUP_TDP_MAX:
        _rt_set(startup_tdp_skipped="above_25w")
        _log(f"startup TDP not applied: {req} W is above {STARTUP_TDP_MAX} W")
        return "above_25w"
    with _tdp_op_lock:
        # Always stock VRM: an unlock is session-only and re-armed by hand.
        ok, err = apply_tdp(req, False, "startup")
        _rt_set(startup_tdp_skipped=None)
        if ok:
            # Only a successful unattended apply makes the persisted lock live.
            _rt_set(tdp_locked=bool(s["tdp_locked"]))
            return "applied"
    _log(f"startup TDP apply failed: {err}")
    return "failed"


# ─────────────────────────────────────────────────────────────────────────────
# Config save (§14 POST /api/config) and alerts (§14 POST /api/alert/test)
# ─────────────────────────────────────────────────────────────────────────────

def save_config(raw):
    """Hand the partial config to fan-config-save.sh; the daemon validates and answers."""
    payload = json.dumps(raw)
    if len(payload.encode("utf-8")) > MAX_BODY:
        return _err("The config is too large.")
    if DRY_RUN:
        _log("dry-run: fan-config-save.sh skipped")
        merged = load_config()
        merged.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG and k != "schema"})
        return {"success": True, "error": None, "config": merged,
                "rejected": sorted(k for k in raw if k not in DEFAULT_CONFIG), "dry_run": True}
    r = _sudo([CFG_SAVE], timeout=30, stdin=payload)
    if r.returncode != 0:
        return _err(_stderr_tail(r, f"fan-config-save.sh exited {r.returncode}"))
    # The daemon prints exactly one JSON line; take the last non-empty one in
    # case sudo or Python ever prints a warning first.
    lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
    try:
        out = _loads_lenient(lines[-1]) if lines else None
    except (ValueError, RecursionError):
        out = None
    if not isinstance(out, dict) or not isinstance(out.get("config"), dict):
        return _err("The daemon saved the config but returned no readable summary.")
    rejected = out.get("rejected")
    return {"success": True, "error": None, "config": out["config"],
            "rejected": [str(k) for k in rejected] if isinstance(rejected, list) else []}


_alert_last = [-1e9]


def _alert_sound_path():
    p = load_config().get("alert_sound")
    if isinstance(p, str) and os.path.isabs(p) and os.access(p, os.R_OK):
        return p
    return FALLBACK_WAV if os.access(FALLBACK_WAV, os.R_OK) else REPO_WAV


def _play_local(path):
    """paplay first; if it fails within 2 s, gst-play-1.0 (the daemon's order, §8)."""
    dn = subprocess.DEVNULL
    try:
        p = subprocess.Popen(["paplay", path], stdin=dn, stdout=dn, stderr=dn, start_new_session=True)
        try:
            if p.wait(timeout=2) == 0:
                return
        except subprocess.TimeoutExpired:
            return                               # still playing: it works
    except OSError:
        pass
    try:
        p = subprocess.Popen(["gst-play-1.0", "--no-interactive", "--volume=1.0", path],
                             stdin=dn, stdout=dn, stderr=dn, start_new_session=True)
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
    except OSError as e:
        _log(f"alert playback failed: {e}")


def test_alert():
    reply = daemon_request({"cmd": "test_alert"})
    if reply is not None:
        if reply.get("ok"):
            return {"success": True, "error": None, "fallback": False}
        return _err(_daemon_error_text(reply), fallback=False, code=reply.get("error"))
    now = time.monotonic()
    if now - _alert_last[0] < 10:
        return _err("Please wait a few seconds between test alerts.", code="rate_limited")
    _alert_last[0] = now
    path = _alert_sound_path()
    if DRY_RUN:
        _log(f"dry-run: local playback of {path} skipped")
        return {"success": True, "error": None, "fallback": True}
    threading.Thread(target=_play_local, args=(path,), name="alert", daemon=True).start()
    return {"success": True, "error": None, "fallback": True}


# ─────────────────────────────────────────────────────────────────────────────
# Status (§15) and the single mode() derivation
# ─────────────────────────────────────────────────────────────────────────────

_REASON_TO_MODE = {
    "critical": "critical",
    "override": "hold",
    "override_suspended_hot": "hold_suspended",
    "sensor_lost": "sensor_lost",
}


def mode(daemon_active_flag, level, state):
    """
    §15, the only place the mode word is derived (the tray uses it too).
    Without the daemon, an unreadable /proc level (None) counts as firmware:
    nothing can be "manual" when the fan interface itself is absent.
    """
    if not daemon_active_flag:
        return "firmware" if level in (None, "auto") else "manual_unprotected"
    reason = state.get("reason") if isinstance(state, dict) else None
    return _REASON_TO_MODE.get(reason, "curve")


def fmt_duration(seconds):
    """Menu label for a hold duration: 0 → 'until resumed'."""
    s = _parse_int(seconds)
    if s is None:
        return "?"
    if s <= 0:
        return "until resumed"
    if s < 3600:
        return f"{max(1, round(s / 60))} min"
    h, m = divmod(round(s / 60), 60)
    return f"{h} h" if m == 0 else f"{h} h {m} min"


def _fan_fields(fp, state, active):
    """(level, speed, fan_control_available, fan_status) from /proc, or from a live state.json
    when /proc cannot be read by this user."""
    if fp["readable"] or not active:
        return fp["level"], fp["speed"], fp["control_available"], fp["status"]
    lvl, rpm = state.get("level_proc"), state.get("rpm")
    avail, status = state.get("fan_control_available"), state.get("fan_status")
    return (lvl if isinstance(lvl, str) else None,
            rpm if _is_int(rpm) else None,
            avail if isinstance(avail, bool) else False,
            status if isinstance(status, str) else None)


def get_status():
    temps = read_temps()
    state = read_state()
    active = daemon_active(state)
    age = state_age(state)
    level, speed, fan_ok, fan_status = _fan_fields(read_fan_proc(), state, active)
    rpm = read_fan_rpm()
    if rpm is None:
        rpm = speed
    smu = SMU.get()
    power = read_power()
    settings = get_settings()
    caps = tdp_caps()

    raw = _num(state.get("temp_raw")) if active else None
    if raw is not None:
        temp_c = round(float(raw), 1)
    else:
        vals = [v for v in (temps["cpu"], temps["igpu"]) if v is not None]
        temp_c = max(vals) if vals else None

    if active and _rt("hold_owned") and not _owns_override(state):
        # Our hold expired, was resumed elsewhere or replaced by another client.
        _rt_set(hold_owned=False, owned_set_at=None)

    vrm_hw = vrm_unlocked_hw(smu)
    with _rt_lock:
        rt = dict(_RT)
    fb_active = FALLBACK.active

    return {
        "version": VERSION,
        "ts": time.time(),
        "temps": temps,
        "temp_c": temp_c,
        "fan_rpm": rpm,
        "fan1_rpm": rpm,                               # alias, one release
        "level": level,
        "speed": speed,
        "fan_control_available": fan_ok,
        "fan_enabled": fan_ok,                         # compatibility alias of fan_control_available
        "fan_status": fan_status,                      # raw EC word; "disabled" = level 0, not an error
        "cpu_mhz_avg": read_cpu_mhz_avg(),
        "gpu_mhz": read_gpu_mhz(),
        "gpu_busy": read_gpu_busy(),
        "loadavg1": read_loadavg1(),
        "governor": read_governor(),
        "boost": read_boost(),
        "on_ac": power["on_ac"],
        "battery_pct": power["battery_pct"],
        "battery_status": power["battery_status"],
        "battery_watts": power["battery_watts"],
        "daemon_active": active,
        "daemon_enabled": DAEMON_ENABLED.get(),
        # Only a live state is handed out: a stale one would read as the
        # daemon's current decision. Its age still shows "stopped N s ago".
        "state": state if active else None,
        "state_age_s": None if age is None else round(age, 2),
        "mode": mode(active, level, state),
        "manual_fallback": fb_active,
        "manual_critical": bool(fb_active and FALLBACK.manual_critical),
        "manual_fallback_level": FALLBACK.level if fb_active else None,
        "manual_fallback_remaining_s": FALLBACK.remaining_s() if fb_active else None,
        "smu": smu,
        "tdp": smu["tdp"],
        "power": smu["power"],
        "edc": smu["edc"],
        "edc_limit": smu["edc_limit"],
        "tdc": smu["tdc"],
        "tdc_limit": smu["tdc_limit"],
        "thm_limit": smu["thm_limit"],
        "tdp_requested": settings["tdp_requested"],
        "tdp_locked": rt["tdp_locked"],
        "tdp_lock_paused_thermal": bool(rt["tdp_locked"] and rt["tdp_lock_paused_thermal"]),
        "apply_tdp_at_startup": settings["apply_tdp_at_startup"],
        "startup_tdp_skipped": rt["startup_tdp_skipped"],
        "vrm_unlocked": vrm_hw,
        "vrm_desired": rt["vrm_desired"],
        "tdp_max": caps["tdp_max"],
        "tdp_max_vrm_unlocked": caps["tdp_max_vrm_unlocked"],
        "freeze_config_warning": bool(vrm_hw and smu["tdp"] is not None and smu["tdp"] >= FREEZE_WARN_TDP),
        "critical_temp": critical_temp_now(state),
        "config_mtime": config_mtime(),
        "hold_default_seconds": settings["ui"]["hold_default_seconds"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# History (§14 /api/history): the daemon's samples merged with the backend's
# 2 s sampler. history.json carries no per-sensor temperatures, so the
# sampler records cpu/igpu next to tdp/power.
# ─────────────────────────────────────────────────────────────────────────────

BACKEND_SAMPLES = collections.deque(maxlen=BACKEND_SAMPLES_MAX)
_samples_lock = threading.Lock()


def sample_backend():
    """Append one {t, tdp, power, cpu, igpu}; the SMU values are copied, never refreshed."""
    smu = SMU.snapshot()
    temps = read_temps()
    with _samples_lock:
        BACKEND_SAMPLES.append({
            "t": time.time(),
            "tdp": smu["tdp"] if smu["ok"] else None,
            "power": smu["power"] if smu["ok"] else None,
            "cpu": temps["cpu"],
            "igpu": temps["igpu"],
        })


def sampler_loop(stop_event):
    while True:
        try:
            sample_backend()
        except Exception as e:                   # noqa: BLE001 — a bad read must not end the chart
            _log_once("sampler", f"history sampler: {e!r}")
        if stop_event.wait(BACKEND_SAMPLE_S):
            return


def _parse_minutes(v):
    m = _parse_int(v, digits=4)
    return HISTORY_MAX_MIN if m is None else max(1, min(HISTORY_MAX_MIN, m))


def get_history(minutes=HISTORY_MAX_MIN):
    out = {"samples": [], "events": []}
    if not daemon_active():
        return out
    hist = read_history()
    if not hist:
        return out
    cutoff = time.time() - _parse_minutes(minutes) * 60
    with _samples_lock:
        backend = list(BACKEND_SAMPLES)
    bt = [b["t"] for b in backend]

    def nearest(ts):
        i = bisect.bisect_left(bt, ts)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(bt) and abs(bt[j] - ts) <= HISTORY_MATCH_S:
                if best is None or abs(bt[j] - ts) < abs(bt[best] - ts):
                    best = j
        return backend[best] if best is not None else {}

    samples = hist.get("samples")
    for row in samples if isinstance(samples, list) else []:
        if not isinstance(row, list) or len(row) < 7:
            continue
        ts = _num(row[0])
        if ts is None or ts < cutoff:
            continue
        b = nearest(ts)
        ac = row[6]
        out["samples"].append({
            "t": ts,
            "cpu": b.get("cpu"),
            "igpu": b.get("igpu"),
            "ctrl": _num(row[1]),
            "fast": _num(row[2]),
            "slow": _num(row[3]),
            "rpm": _num(row[5]),
            "level": row[4] if isinstance(row[4], str) else (str(row[4]) if _is_int(row[4]) else None),
            "ac": ac if isinstance(ac, bool) else (bool(ac) if _is_int(ac) else None),
            "tdp": b.get("tdp"),
            "power": b.get("power"),
        })
    events = hist.get("events")
    for ev in events if isinstance(events, list) else []:
        if not isinstance(ev, list) or len(ev) < 3:
            continue
        ts = _num(ev[0])
        if ts is None or ts < cutoff:
            continue
        out["events"].append({"t": ts, "kind": str(ev[1]), "detail": "" if ev[2] is None else str(ev[2])})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Log tail (§10 readers: strip NUL, decode with errors="replace")
# ─────────────────────────────────────────────────────────────────────────────

def _parse_lines(v):
    n = _parse_int(v, digits=6)
    return LOG_DEFAULT_LINES if n is None else max(1, min(LOG_MAX_LINES, n))


def tail_log(lines=LOG_DEFAULT_LINES):
    """Last N non-empty lines. Reads backwards in 64 KiB blocks, bounded by raw bytes read."""
    want = _parse_lines(lines)
    data, raw_read = b"", 0
    try:
        with open(LOG_FILE, "rb") as f:
            pos = f.seek(0, os.SEEK_END)
            while pos > 0 and data.count(b"\n") <= want and raw_read < LOG_MAX_READ_BYTES:
                step = min(65536, pos)
                pos -= step
                f.seek(pos)
                block = f.read(step)
                raw_read += len(block)
                data = block.replace(b"\x00", b"") + data
    except OSError:
        return ""
    rows = data.decode("utf-8", errors="replace").splitlines()
    if pos > 0 and rows:
        rows = rows[1:]                          # the first row started mid-line
    out = [r for r in rows if r.strip()][-want:]
    return "\n".join(out) + ("\n" if out else "")


# ─────────────────────────────────────────────────────────────────────────────
# Settings API (§14 GET/POST /api/settings)
# ─────────────────────────────────────────────────────────────────────────────

def settings_view():
    s = get_settings()
    s["autostart_enabled"] = read_autostart()    # read-only extra, never persisted
    return s


def apply_settings_patch(body):
    """
    Only ui.*, apply_tdp_at_startup and autostart_enabled may be changed here
    (tdp_requested / tdp_locked have their own endpoints, last_clean_exit is
    lifecycle, vrm_* is never stored). All-or-nothing: one invalid value
    rejects the whole request. Booleans must be JSON booleans.
    """
    errors, patch, ignored = [], {}, []
    for k in body:
        if k not in ("ui", "apply_tdp_at_startup", "autostart_enabled"):
            ignored.append(k)
    if "ui" in body:
        ui = body["ui"]
        if not isinstance(ui, dict):
            errors.append("'ui' must be an object.")
        else:
            errors += list(_ui_errors(ui).values())
            known = {k: v for k, v in ui.items() if k in DEFAULT_SETTINGS["ui"]}
            ignored += [f"ui.{k}" for k in ui if k not in DEFAULT_SETTINGS["ui"]]
            if known:
                patch["ui"] = known
    if "apply_tdp_at_startup" in body:
        if isinstance(body["apply_tdp_at_startup"], bool):
            patch["apply_tdp_at_startup"] = body["apply_tdp_at_startup"]
        else:
            errors.append("'apply_tdp_at_startup' must be true or false.")
    autostart = body.get("autostart_enabled")
    if "autostart_enabled" in body and not isinstance(autostart, bool):
        errors.append("'autostart_enabled' must be true or false.")
    if errors:
        return _err(" ".join(errors), settings=settings_view())
    if isinstance(autostart, bool):
        ok, err = set_autostart(autostart)
        if not ok:
            return _err(err, settings=settings_view())
    if patch:
        update_settings(patch)
    return {"success": True, "error": None, "settings": settings_view(), "ignored": ignored}


# ─────────────────────────────────────────────────────────────────────────────
# HTTP API (§14)
#
# Gates, in order: Host allowlist (421, defeats DNS rebinding) → for POST:
# Content-Type application/json (415: a cross-origin JSON POST needs a CORS
# preflight, which never succeeds here), Origin (403), Sec-Fetch-Site (403),
# Content-Length (400 / 413 before reading). OPTIONS is always 403 and no
# Access-Control-Allow-* header is ever sent.
# ─────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 10                         # a client that stalls mid-request is dropped
    server_version = "fanctl/" + VERSION
    sys_version = ""

    # ── response helpers ─────────────────────────────────────────────────────

    def end_headers(self):
        # Added here, not per response, so even the stdlib's own send_error()
        # replies (bad request line, 501 for unknown methods) carry them.
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

    def _json(self, data, code=200):
        try:
            body = json.dumps(data, allow_nan=False, default=str)
        except (ValueError, TypeError, RecursionError) as e:
            # Callers send outside their own try blocks; an unencodable reply
            # must still answer instead of killing the handler thread silently.
            _log(f"{self.command} {self.path}: reply not encodable: {e!r}")
            code, body = 500, json.dumps({"success": False,
                                          "error": "Internal error: the reply could not be encoded."})
        self._send(body, "application/json; charset=utf-8", code=code)

    def _empty(self, code):
        # Any unread request body would corrupt keep-alive framing.
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _reject(self, code, message):
        self.close_connection = True
        self._json({"success": False, "error": message}, code=code)

    # ── gates ────────────────────────────────────────────────────────────────

    def _allowed_hosts(self):
        p = self.server.server_port
        return (f"127.0.0.1:{p}", f"localhost:{p}")

    def _host_ok(self):
        # Exact host:port match. A rebound attacker name still arrives as
        # "attacker.example:7070". Host names are case-insensitive.
        return (self.headers.get("Host") or "").strip().lower() in self._allowed_hosts()

    def _post_gates(self):
        """True when the POST may proceed; otherwise the rejection has been sent."""
        ctype = (self.headers.get("Content-Type") or "").strip().lower()
        if not ctype.startswith("application/json"):
            self._reject(415, "POST bodies must be sent as application/json.")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() not in tuple("http://" + h for h in self._allowed_hosts()):
            self._reject(403, "Cross-origin requests are not accepted.")
            return False
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs.strip().lower() not in ("same-origin", "none"):
            self._reject(403, "Cross-site requests are not accepted.")
            return False
        return True

    def _read_body(self):
        """The parsed JSON object, or None after an error reply was sent."""
        if self.headers.get("Transfer-Encoding"):
            # Without Content-Length the body can be neither bounded nor framed.
            self._reject(400, "Chunked request bodies are not supported; send Content-Length.")
            return None
        try:
            n = int((self.headers.get("Content-Length") or "0").strip())
        except ValueError:
            self._reject(400, "Content-Length is not a number.")
            return None
        if n < 0:
            self._reject(400, "Content-Length is negative.")
            return None
        if n > MAX_BODY:
            self._reject(413, f"The request body is larger than {MAX_BODY} bytes.")
            return None
        raw = self.rfile.read(n) if n else b""
        if len(raw) != n:
            self._reject(400, "The request body is shorter than its Content-Length.")
            return None
        if not raw.strip():
            return {}
        try:
            body = _loads_strict(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            self._json({"success": False, "error": "The request body is not valid JSON."}, code=400)
            return None
        if not isinstance(body, dict):
            self._json({"success": False, "error": "The request body must be a JSON object."}, code=400)
            return None
        return body

    # ── methods ──────────────────────────────────────────────────────────────

    def do_OPTIONS(self):
        if not self._host_ok():
            return self._empty(421)
        self._reject(403, "OPTIONS is not supported.")

    def _method_not_allowed(self):
        if not self._host_ok():
            return self._empty(421)
        self.close_connection = True
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD, POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_PUT = do_DELETE = do_PATCH = do_TRACE = do_CONNECT = _method_not_allowed

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._empty(421)
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)
        try:
            if path in ("/", "/index.html"):
                try:
                    with open(HTML_FILE, "rb") as f:
                        html = f.read()
                except OSError:
                    return self._send("index.html is missing\n", "text/plain; charset=utf-8", code=404)
                return self._send(html, "text/html; charset=utf-8", csp=True)
            if path == "/api/status":
                return self._json(get_status())
            if path == "/api/config":
                return self._json(load_config())
            if path == "/api/history":
                return self._json(get_history((qs.get("minutes") or [HISTORY_MAX_MIN])[0]))
            if path == "/api/log":
                return self._send(tail_log((qs.get("lines") or [LOG_DEFAULT_LINES])[0]),
                                  "text/plain; charset=utf-8")
            if path == "/api/settings":
                return self._json(settings_view())
        except Exception as e:                   # noqa: BLE001 — never kill the handler thread
            _log(f"GET {path} failed: {e!r}")
            return self._json({"success": False, "error": f"Internal error: {e}"}, code=500)
        if path.startswith("/api/"):
            return self._json({"success": False, "error": "Unknown API path."}, code=404)
        self._empty(404)

    def do_POST(self):
        if not self._host_ok():
            return self._empty(421)
        if not self._post_gates():
            return
        body = self._read_body()
        if body is None:
            return
        path = urlparse(self.path).path
        try:
            result = self._route_post(path, body)
        except Exception as e:                   # noqa: BLE001
            _log(f"POST {path} failed: {e!r}")
            result = _err(f"Internal error: {e}")
        if result is None:
            return self._json({"success": False, "error": "Unknown API path."}, code=404)
        self._json(result)

    @staticmethod
    def _route_post(path, body):
        if path == "/api/fan/hold":
            return fan_hold(body.get("level"), body.get("seconds"))
        if path == "/api/fan/resume":
            return fan_resume()
        if path == "/api/fan/set":               # legacy alias of hold (§14)
            return fan_hold(body.get("level"), get_settings()["ui"]["hold_default_seconds"])
        if path in ("/api/daemon/start", "/api/daemon/stop", "/api/daemon/restart"):
            return daemon_ctrl(path.rsplit("/", 1)[1])
        if path == "/api/tdp/set":
            return set_tdp(body.get("tdp"))
        if path == "/api/tdp/lock":
            return set_tdp_lock(body.get("locked"))
        if path == "/api/tdp/vrm_unlock":
            return set_vrm_unlock(body.get("unlocked"))
        if path == "/api/tdp/restore_stock":
            return restore_stock()
        if path == "/api/config":
            return save_config(body)
        if path == "/api/settings":
            return apply_settings_patch(body)
        if path == "/api/alert/test":
            return test_alert()
        return None

    def log_message(self, *_):
        pass                                     # the access log is noise here


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle
# ─────────────────────────────────────────────────────────────────────────────

_stop_event = threading.Event()
_workers = []


def make_server(port=PORT):
    """Bind 127.0.0.1:port. Raises OSError (EADDRINUSE) when another copy owns it."""
    if not _is_int(port) or not 1 <= port <= 65535:
        # bind() would raise OverflowError, which callers expecting OSError
        # would turn into a traceback.
        raise OSError(errno.EINVAL, f"invalid port {port!r}")
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    return srv


def _begin_session():
    """§9: note whether the previous session exited cleanly, then mark this one unclean."""
    s = get_settings()
    clean = bool(s["last_clean_exit"])
    update_settings({"last_clean_exit": False})
    return s, clean


def start_workers():
    """Session start, TDP lock loop (with the startup rule first), history sampler. Idempotent."""
    global _stop_event
    with _rt_lock:
        if _RT["workers_started"]:
            return
        _RT["workers_started"] = True
        _RT["released"] = False
        # A fresh Event per start: clearing the old one would revive workers
        # of a previous start that have not noticed their stop yet.
        _stop_event = stop = threading.Event()
    startup = _begin_session()
    _workers[:] = [
        threading.Thread(target=tdp_lock_loop, args=(stop, startup), name="tdp-lock", daemon=True),
        threading.Thread(target=sampler_loop, args=(stop,), name="history-sampler", daemon=True),
    ]
    for t in _workers:
        t.start()


def stop_workers():
    _stop_event.set()
    _rt_set(workers_started=False)


def start_background(port=PORT):
    """Bind, serve on a daemon thread, start the workers. Raises OSError when the port is taken."""
    srv = make_server(port)
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    start_workers()
    return srv


def release_all():
    """
    The shared quit path (§16), safe to call more than once: resume an
    override this process created, end a daemon-less hold with `auto 0`,
    restore stock power limits if this session unlocked the VRM, then mark
    the exit clean. Blocking (sudo): app.py runs it off the GTK thread.
    """
    stop_workers()          # a lock re-apply is refused from now on (checked under _tdp_op_lock)
    with _rt_lock:
        first = not _RT["released"]
        _RT["released"] = True
    if first:
        try:
            st = read_state()
            if daemon_active(st) and isinstance(st.get("override"), dict) and _owns_override(st):
                r = fan_resume()
                _log("quit: resumed the daemon's curve" if r["success"] else f"quit: resume failed: {r['error']}")
        except Exception as e:                   # noqa: BLE001 — the other steps must still run
            _log(f"quit: resume: {e!r}")
        try:
            if FALLBACK.active:
                FALLBACK.stop()
                if not daemon_active():
                    ok, err = _fan_set_direct("auto", 0)
                    _log("quit: fan back to firmware auto" if ok else f"quit: auto write failed: {err}")
        except Exception as e:                   # noqa: BLE001
            _log(f"quit: fallback: {e!r}")
        try:
            if _rt("vrm_desired"):
                r = restore_stock()
                _log("quit: stock power limits restored" if r["success"]
                     else f"quit: restore stock failed: {r['error']}")
        except Exception as e:                   # noqa: BLE001
            _log(f"quit: restore stock: {e!r}")
    try:
        update_settings({"last_clean_exit": True})
    except Exception as e:                       # noqa: BLE001
        _log(f"quit: settings: {e!r}")
