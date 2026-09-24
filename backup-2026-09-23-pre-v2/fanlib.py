#!/usr/bin/env python3
"""
ThinkPad Fan Control — shared backend.

Everything that touches hardware or the daemon lives here, so app.py (GTK) and
server.py (browser) are both thin shells over the same code.

Target hardware: ThinkPad T495 (Ryzen 3x00U "Picasso"), thinkpad_acpi driver.
"""

import glob
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

FAN_PROC    = "/proc/acpi/ibm/fan"
LOG_FILE    = "/var/log/thinkpad-fan-control.log"
CONFIG_FILE = "/etc/thinkpad-fan-control/config.json"
HTML_FILE   = os.path.join(SCRIPT_DIR, "index.html")

FAN_SET     = "/usr/local/bin/fan-set-level.sh"
TDP_SET     = "/usr/local/bin/ryzenadj-set-tdp.sh"
CFG_SAVE    = "/usr/local/bin/fan-config-save.sh"
RYZENADJ    = "/usr/local/bin/ryzenadj"
SERVICE     = "thinkpad-fan-control.service"

PORT = 7070

# The T495's fan tops out around here; used only to scale the RPM bars.
MAX_RPM = 6300

VALID_LEVELS = {"auto", "disengaged", "full-speed"} | {str(i) for i in range(8)}

DEFAULT_CONFIG = {
    "poll_interval": 3,
    "hysteresis": 5,
    "critical_temp": 90,
    "curve": [
        {"temp": 0,  "level": "auto"},
        {"temp": 50, "level": "4"},
        {"temp": 60, "level": "6"},
        {"temp": 70, "level": "7"},
        {"temp": 80, "level": "disengaged"},
    ],
    "battery_curve": [
        {"temp": 0,  "level": "auto"},
        {"temp": 60, "level": "3"},
        {"temp": 70, "level": "5"},
        {"temp": 80, "level": "7"},
        {"temp": 87, "level": "disengaged"},
    ],
    "use_battery_curve": True,
    "alerts_enabled": True,
    "alert_sound": "/home/jhnlstrlclcn/Music/SYSTEM SOUND/90c.mp3",
    "alert_cooldown": 300,
    "watchdog": 0,
}


# ─────────────────────────────────────────────────────────────────────────────
# Low-level file access
# ─────────────────────────────────────────────────────────────────────────────

def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def read_int(path, default=0):
    v = read_file(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# hwmon discovery
#
# hwmon numbering is assigned in module-probe order and genuinely does shuffle
# between boots, so every sensor is looked up by its driver name instead of a
# hardcoded hwmonN path. Results are cached but re-resolved if a path vanishes.
# ─────────────────────────────────────────────────────────────────────────────

class HwmonIndex:
    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def _scan(self):
        found = {}
        for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            name = read_file(os.path.join(d, "name"))
            if name and name not in found:
                found[name] = d
        return found

    def dir_for(self, name):
        """Path of the hwmon directory for a driver name, or None."""
        with self._lock:
            cached = self._cache.get(name)
            if cached and os.path.isdir(cached):
                if read_file(os.path.join(cached, "name")) == name:
                    return cached
            self._cache = self._scan()
            return self._cache.get(name)

    def attr(self, name, attr):
        d = self.dir_for(name)
        return os.path.join(d, attr) if d else None

    def temp(self, name, attr="temp1_input"):
        """Temperature in whole °C, or None if the sensor is absent."""
        p = self.attr(name, attr)
        if not p:
            return None
        raw = read_file(p)
        try:
            return int(raw) // 1000
        except (TypeError, ValueError):
            return None


HWMON = HwmonIndex()


def read_temps():
    """All temperatures we care about. Missing sensors come back as None."""
    return {
        "cpu":  HWMON.temp("k10temp"),      # Tctl — the control signal
        "igpu": HWMON.temp("amdgpu"),       # Vega edge temperature
        "nvme": HWMON.temp("nvme"),
        "wifi": HWMON.temp("iwlwifi_1_0"),
    }


def read_fans():
    d = HWMON.dir_for("thinkpad")
    if not d:
        return 0, 0
    return (read_int(os.path.join(d, "fan1_input")),
            read_int(os.path.join(d, "fan2_input")))


def read_fan_proc():
    """Parse /proc/acpi/ibm/fan → (level, speed, enabled)."""
    info = read_file(FAN_PROC) or ""
    level, speed, enabled = "unknown", 0, False
    for line in info.splitlines():
        if line.startswith("level:"):
            level = line.split()[-1]
        elif line.startswith("speed:"):
            try:
                speed = int(line.split()[-1])
            except ValueError:
                pass
        elif line.startswith("status:"):
            enabled = line.split()[-1] == "enabled"
    return level, speed, enabled


def read_power():
    """AC/battery state. Fields are None when the kernel doesn't expose them."""
    ac = read_file("/sys/class/power_supply/AC/online")
    bat = "/sys/class/power_supply/BAT0"
    watts = None
    power_now = read_int(os.path.join(bat, "power_now"), -1)
    if power_now > 0:
        watts = round(power_now / 1_000_000, 1)
    else:
        # Some firmware exposes current+voltage instead of power.
        cur = read_int(os.path.join(bat, "current_now"), -1)
        vol = read_int(os.path.join(bat, "voltage_now"), -1)
        if cur > 0 and vol > 0:
            watts = round(cur * vol / 1e12, 1)
    return {
        "on_ac":       ac == "1" if ac is not None else True,
        "battery_pct": read_int(os.path.join(bat, "capacity"), -1),
        "battery_status": read_file(os.path.join(bat, "status")),
        "battery_watts": watts,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cached subprocess calls
#
# /api/status is polled once a second; without these the GUI would fork
# ryzenadj (which pokes /dev/mem) and systemctl on every single request.
# ─────────────────────────────────────────────────────────────────────────────

class Cached:
    def __init__(self, fn, ttl, initial=None):
        self.fn, self.ttl = fn, ttl
        self.value, self.at = initial, 0.0
        self.lock = threading.Lock()

    def get(self, force=False):
        with self.lock:
            now = time.monotonic()
            if force or now - self.at >= self.ttl:
                try:
                    self.value = self.fn()
                except Exception as e:
                    print(f"  [ERR] {self.fn.__name__}: {e}")
                self.at = now
            return self.value

    def poke(self, value):
        """Record a value we just caused, so the UI doesn't lag a refresh."""
        with self.lock:
            self.value = value


_SMU_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|")


def _ryzenadj_info():
    r = subprocess.run(["sudo", "-n", RYZENADJ, "--info"],
                       capture_output=True, text=True, timeout=6)
    vals = {}
    for line in r.stdout.splitlines():
        m = _SMU_ROW.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        try:
            vals[key] = float(raw)
        except ValueError:
            pass          # "nan", or the header row

    def g(key, default=None):
        v = vals.get(key)
        return None if v is None or v != v else v      # v != v filters NaN

    return {
        "tdp":        int(g("STAPM LIMIT", 22) or 22),
        "power":      round(g("PPT VALUE FAST", 0.0) or 0.0, 1),
        "power_slow": round(g("PPT VALUE SLOW", 0.0) or 0.0, 1),
        "edc":        round(g("EDC VALUE VDD", 0.0) or 0.0, 1),
        "edc_limit":  round(g("EDC LIMIT VDD", 0.0) or 0.0, 1),
        "tdc":        round(g("TDC VALUE VDD", 0.0) or 0.0, 1),
        "tdc_limit":  round(g("TDC LIMIT VDD", 0.0) or 0.0, 1),
        "thm_limit":  round(g("THM LIMIT CORE", 0.0) or 0.0, 1),
    }


def _daemon_active():
    r = subprocess.run(["systemctl", "is-active", SERVICE],
                       capture_output=True, text=True, timeout=5)
    return r.stdout.strip() == "active"


SMU    = Cached(_ryzenadj_info, ttl=4.0, initial={
    "tdp": 22, "power": 0.0, "power_slow": 0.0,
    "edc": 0.0, "edc_limit": 0.0, "tdc": 0.0, "tdc_limit": 0.0, "thm_limit": 0.0})
DAEMON = Cached(_daemon_active, ttl=3.0, initial=False)


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

def _valid_curve(curve):
    if not isinstance(curve, list) or not 1 <= len(curve) <= 8:
        return None
    out = []
    for step in curve:
        try:
            t = int(step["temp"])
            lvl = str(step["level"])
        except (TypeError, KeyError, ValueError):
            return None
        if not 0 <= t <= 105 or lvl not in VALID_LEVELS:
            return None
        out.append({"temp": t, "level": lvl})
    out.sort(key=lambda s: s["temp"])
    out[0]["temp"] = 0                      # the base step always starts at 0
    if len({s["temp"] for s in out}) != len(out):
        return None                          # duplicate thresholds
    return out


def sanitize_config(raw, base=None):
    """
    Clamp anything a client sends into a range that can't cook the laptop.

    Missing or invalid fields fall back to `base` (normally the config already
    on disk) so a partial write can't reset settings the caller didn't mention.
    The daemon re-runs the same checks before writing; this copy just keeps the
    GUI from sending something it already knows is bad.
    """
    cfg = json.loads(json.dumps(base if base else DEFAULT_CONFIG))
    if not isinstance(raw, dict):
        return cfg
    for key, lo, hi in (("poll_interval", 1, 60),
                        ("hysteresis", 0, 20),
                        ("critical_temp", 70, 105),
                        ("alert_cooldown", 30, 3600),
                        ("watchdog", 0, 120)):
        try:
            cfg[key] = max(lo, min(hi, int(raw[key])))
        except (KeyError, TypeError, ValueError):
            pass
    for key in ("use_battery_curve", "alerts_enabled"):
        if key in raw:
            cfg[key] = bool(raw[key])
    if isinstance(raw.get("alert_sound"), str):
        cfg["alert_sound"] = raw["alert_sound"][:400]
    for key in ("curve", "battery_curve"):
        c = _valid_curve(raw.get(key))
        if c:
            cfg[key] = c
    return cfg


def load_config():
    raw = read_file(CONFIG_FILE)
    if not raw:
        return dict(DEFAULT_CONFIG)
    try:
        return sanitize_config(json.loads(raw))
    except json.JSONDecodeError:
        return dict(DEFAULT_CONFIG)


def save_config(raw):
    """Hand the config to the root-owned wrapper, which re-validates it."""
    cfg = sanitize_config(raw, base=load_config())
    try:
        r = subprocess.run(["sudo", "-n", CFG_SAVE],
                           input=json.dumps(cfg), capture_output=True,
                           text=True, timeout=6)
        if r.returncode != 0:
            print(f"  [ERR] save_config: {r.stderr.strip()}")
        return r.returncode == 0
    except Exception as e:
        print(f"  [ERR] save_config: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Actuators
# ─────────────────────────────────────────────────────────────────────────────

# Set once the GUI takes manual control, so we know to hand the fan back to the
# firmware on exit rather than leaving it pinned at whatever the user picked.
manual_override = False

tdp_locked   = False
vrm_unlocked = False


def set_fan_level(level):
    global manual_override
    level = str(level)
    if level not in VALID_LEVELS:
        return False
    try:
        r = subprocess.run(["sudo", "-n", FAN_SET, level],
                           capture_output=True, text=True, timeout=6)
        if r.returncode == 0:
            manual_override = level != "auto"
            return True
    except Exception as e:
        print(f"  [ERR] set_fan_level: {e}")
    return False


def release_fan():
    """Return the fan to firmware control. Safe to call more than once."""
    global manual_override
    if manual_override and not DAEMON.get(force=True):
        set_fan_level("auto")
    manual_override = False


def set_tdp(watts):
    try:
        watts = int(watts)
    except (TypeError, ValueError):
        return False
    if not 5 <= watts <= 40:
        return False
    try:
        r = subprocess.run(["sudo", "-n", TDP_SET, str(watts),
                            "1" if vrm_unlocked else "0"],
                           capture_output=True, text=True, timeout=8)
        if r.returncode == 0:
            cur = dict(SMU.get())
            cur["tdp"] = watts
            SMU.poke(cur)
            return True
    except Exception as e:
        print(f"  [ERR] set_tdp: {e}")
    return False


def daemon_ctrl(action):
    if action not in ("start", "stop", "restart"):
        return False
    try:
        r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", action, SERVICE],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            DAEMON.poke(action != "stop")
            return True
    except Exception as e:
        print(f"  [ERR] daemon_ctrl: {e}")
    return False


def tdp_lock_loop():
    """Re-assert the TDP limit periodically; the firmware likes to reset it."""
    while True:
        if tdp_locked:
            try:
                subprocess.run(["sudo", "-n", TDP_SET, str(SMU.get()["tdp"]),
                                "1" if vrm_unlocked else "0"],
                               capture_output=True, text=True, timeout=8)
            except Exception as e:
                print(f"  [ERR] tdp_lock_loop: {e}")
        time.sleep(5)


# ─────────────────────────────────────────────────────────────────────────────
# Status
# ─────────────────────────────────────────────────────────────────────────────

def control_temp(temps):
    """The number the fan curve reacts to: hottest of CPU and iGPU."""
    vals = [t for t in (temps.get("cpu"), temps.get("igpu")) if t]
    return max(vals) if vals else 0


def get_status():
    temps = read_temps()
    fan1, fan2 = read_fans()
    level, speed, enabled = read_fan_proc()
    smu = SMU.get()
    power = read_power()
    return {
        "temp_c":       control_temp(temps),
        "temps":        temps,
        "fan1_rpm":     fan1,
        "fan2_rpm":     fan2,
        "max_rpm":      MAX_RPM,
        "level":        level,
        "speed":        speed,
        "fan_enabled":  enabled,
        "daemon_active": DAEMON.get(),
        "manual":       manual_override,
        "tdp_locked":   tdp_locked,
        "vrm_unlocked": vrm_unlocked,
        **smu,
        **power,
    }


def tail_log(lines=40):
    try:
        r = subprocess.run(["tail", "-n", str(lines), LOG_FILE],
                           capture_output=True, text=True, timeout=5)
        return r.stdout
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# HTTP API
# ─────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, body, ctype):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data):
        self._send(json.dumps(data), "application/json")

    def _404(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(HTML_FILE, "rb") as f:
                    self._send(f.read(), "text/html; charset=utf-8")
            except OSError:
                self._404()
        elif path == "/api/status":
            self._json(get_status())
        elif path == "/api/config":
            self._json(load_config())
        elif path == "/api/log":
            self._send(tail_log(), "text/plain; charset=utf-8")
        else:
            self._404()

    def do_POST(self):
        global tdp_locked, vrm_unlocked
        path = urlparse(self.path).path
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(n).decode()) if n else {}
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        ok = False

        if path == "/api/fan/set":
            ok = set_fan_level(body.get("level", "auto"))
        elif path == "/api/fan/release":
            release_fan()
            ok = True
        elif path == "/api/daemon/start":
            ok = daemon_ctrl("start")
        elif path == "/api/daemon/stop":
            ok = daemon_ctrl("stop")
        elif path == "/api/daemon/restart":
            ok = daemon_ctrl("restart")
        elif path == "/api/tdp/set":
            ok = set_tdp(body.get("tdp"))
        elif path == "/api/tdp/lock":
            tdp_locked = bool(body.get("locked"))
            ok = True
        elif path == "/api/tdp/vrm_unlock":
            vrm_unlocked = bool(body.get("unlocked"))
            ok = set_tdp(SMU.get()["tdp"])
        elif path == "/api/config":
            ok = save_config(body)
        else:
            self._404()
            return

        self._json({"success": ok})

    def log_message(self, *_):
        pass          # the access log is noise here


def make_server(port=PORT):
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def start_background(port=PORT):
    """Serve the API on a daemon thread and start the TDP keep-alive loop."""
    srv = make_server(port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=tdp_lock_loop, daemon=True).start()
    return srv
