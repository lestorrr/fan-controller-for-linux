#!/usr/bin/env python3
"""
Offline tests for fanlib's HTTP API and helpers (docs/CONTRACT.md §9, §14, §15).

Run from the repository root:

    FANCTL_DRY_RUN=1 python3 -m unittest tests/test_fanlib_api.py -v

Nothing here touches the fan, sudo, systemd, ryzenadj or the real daemon:
every path fanlib reads or writes is redirected into a temp dir through the
FANCTL_* variables *before* fanlib is imported, the server binds a free port
in 7100-7199, and FANCTL_DRY_RUN=1 (forced below) turns every actuation into
a logged no-op. /proc/acpi/ibm/fan is replaced by a fixture file; the other
sensor reads (hwmon, /proc/cpuinfo) stay real, so they are checked by type.
"""

import http.client
import json
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
from unittest import mock

# ── environment, before fanlib is imported ───────────────────────────────────

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="fanctl-test-")
if len(os.path.join(TMP, "run", "control.sock")) > 100:        # AF_UNIX path limit is 108
    shutil.rmtree(TMP)
    TMP = tempfile.mkdtemp(prefix="fanctl-test-", dir="/tmp")
RUNTIME = os.path.join(TMP, "run")
os.makedirs(RUNTIME)
ENV = {
    "FANCTL_DRY_RUN": "1",
    "FANCTL_RUNTIME_DIR": RUNTIME,
    "FANCTL_SETTINGS_FILE": os.path.join(TMP, "cfg", "thinkpad-fan-control", "settings.json"),
    "FANCTL_AUTOSTART_FILE": os.path.join(TMP, "autostart", "fan-control.desktop"),
    "FANCTL_ENV_FILE": os.path.join(TMP, "daemon.env"),
    "FANCTL_LOG_FILE": os.path.join(TMP, "daemon.log"),
    "FANCTL_CONFIG_FILE": os.path.join(TMP, "config.json"),
    "FANCTL_HTML_FILE": os.path.join(TMP, "index.html"),
    "FANCTL_FAN_PROC": os.path.join(TMP, "fan"),
}
os.environ.update(ENV)
sys.path.insert(0, REPO)
import fanlib  # noqa: E402

assert fanlib.DRY_RUN, "these tests must run with FANCTL_DRY_RUN=1"
assert not fanlib._runtime_is_production()

STATE = os.path.join(RUNTIME, "state.json")
HISTORY = os.path.join(RUNTIME, "history.json")
SOCK = os.path.join(RUNTIME, "control.sock")

PROC_CONTROL = ("status:\t\tenabled\nspeed:\t\t4100\nlevel:\t\t7\n"
                "commands:\tlevel <level> (<level> is 0-7, auto, disengaged, full-speed)\n"
                "commands:\tenable, disable\n"
                "commands:\twatchdog <timeout> (<timeout> is 0 (off), 1-120 (seconds))\n")
# Fan stopped at level 0: the EC status byte reads "disabled", control is still available.
PROC_LEVEL0 = ("status:\t\tdisabled\nspeed:\t\t0\nlevel:\t\t0\n"
               "commands:\tlevel <level> (<level> is 0-7, auto, disengaged, full-speed)\n"
               "commands:\tenable, disable\n"
               "commands:\twatchdog <timeout> (<timeout> is 0 (off), 1-120 (seconds))\n")
# thinkpad_acpi without fan_control=1: no commands: lines at all.
PROC_NO_CONTROL = "status:\t\tenabled\nspeed:\t\t2900\nlevel:\t\tauto\n"

CSP = "default-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:"

STATUS_KEYS = """version ts temps temp_c fan_rpm fan1_rpm level speed fan_control_available
fan_enabled fan_status cpu_mhz_avg gpu_mhz gpu_busy loadavg1 governor boost on_ac battery_pct
battery_status battery_watts daemon_active daemon_enabled state state_age_s mode manual_fallback
manual_critical smu tdp power edc edc_limit tdc tdc_limit thm_limit tdp_requested tdp_locked
tdp_lock_paused_thermal apply_tdp_at_startup startup_tdp_skipped vrm_unlocked vrm_desired tdp_max
tdp_max_vrm_unlocked freeze_config_warning critical_temp config_mtime hold_default_seconds""".split()
SMU_KEYS = "ok stale tdp power power_slow edc edc_limit tdc tdc_limit thm_limit updated_at".split()
MODES = {"curve", "hold", "hold_suspended", "critical", "firmware", "manual_unprotected", "sensor_lost"}

SMU_TABLE = """\
CPU Family: Picasso
SMU BIOS Interface Version: 5
Version: v0.19.0
PM Table Version: 1e0004
|        Name         |   Value   |     Parameter      |
|---------------------|-----------|--------------------|
| STAPM LIMIT         |    29.000 | stapm-limit        |
| STAPM VALUE         |     8.312 |                    |
| PPT LIMIT FAST      |    29.000 | fast-limit         |
| PPT VALUE FAST      |    11.873 |                    |
| PPT LIMIT SLOW      |    29.000 | slow-limit         |
| PPT VALUE SLOW      |     9.505 |                    |
| TDC LIMIT VDD       |    35.000 | vrm-current        |
| TDC VALUE VDD       |    10.110 |                    |
| TDC LIMIT SOC       |       nan | vrmsoc-current     |
| EDC LIMIT VDD       |    45.000 | vrmmax-current     |
| EDC VALUE VDD       |    40.742 |                    |
| THM LIMIT CORE      |    95.000 | tctl-temp          |
| THM VALUE CORE      |    71.438 |                    |
"""


def write(path, text, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        f.write(text)


def write_json(path, obj):
    write(path, json.dumps(obj))


def rm(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def fresh_state(**over):
    """A §5 state.json that passes the liveness rule."""
    now = time.time()
    st = {
        "schema": 1, "version": "2.0.0", "pid": 4242, "ts": now, "mono": 1234.5,
        "sample_interval": 1, "sensor_ok": True, "temp_raw": 71.44, "temp_fast": 70.2,
        "temp_slow": 68.9, "cpu": 71.44, "igpu": 70.0, "level": "7", "level_proc": "7",
        "rpm": 4000, "fan_control_available": True, "fan_status": "enabled", "reason": "curve",
        "on_ac": True, "curve": "ac", "step_index": 3, "steps": 5, "curve_level": "7",
        "up_at": 80, "down_below": 64, "level_since": now - 60, "dwell_remaining_s": 0,
        "critical": {"active": False, "since": None, "alerts": 0}, "override": None,
        "watchdog": 60, "hysteresis": 6, "critical_temp": 90, "config_mtime": 1.0,
        "config_schema": 2, "rpm_by_level": {"7": 4000, "disengaged": 6400},
        "top_cycles_10min": 0, "external_write_detected": False,
    }
    st.update(over)
    return st


def override_obj(level="3", remaining=600, set_at=None):
    now = time.time()
    set_at = now - 5 if set_at is None else set_at
    return {"level": level, "until": now + remaining, "set_at": set_at,
            "remaining_s": remaining, "suspended": False}


def daemon_up(**over):
    write_json(STATE, fresh_state(**over))


def daemon_down():
    rm(STATE)
    rm(HISTORY)


class FakeDaemon:
    """Minimal §7 server: one JSON line in, one JSON line out; records every request."""

    def __init__(self, path=SOCK):
        self.path = path
        self.requests = []
        self.replies = {}                       # cmd → reply dict (default: ok + state)
        rm(path)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(path)
        self._srv.listen(4)
        self._srv.settimeout(0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                conn.settimeout(2)
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.split(b"\n")[0])
                self.requests.append(req)
                reply = self.replies.get(req.get("cmd"))
                if reply is None:
                    st = fresh_state(reason="override", override=override_obj(req.get("level", "3")))
                    if req.get("cmd") == "resume":
                        st = fresh_state()
                    reply = {"ok": True, "state": st} if req.get("cmd") != "test_alert" else {"ok": True}
                conn.sendall((json.dumps(reply) + "\n").encode())

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self._srv.close()
        rm(self.path)


# ── server under test ────────────────────────────────────────────────────────

PORT = None
SERVER = None


def setUpModule():
    global PORT, SERVER
    write(ENV["FANCTL_HTML_FILE"], "<!doctype html><title>test dashboard</title>")
    write(ENV["FANCTL_ENV_FILE"], "FANCTL_GUI_UID=1000\nFANCTL_TDP_MAX=35\nFANCTL_TDP_MAX_VRM_UNLOCKED=30\n")
    write(ENV["FANCTL_FAN_PROC"], PROC_CONTROL)
    for port in range(7100, 7200):
        try:
            SERVER = fanlib.make_server(port)
        except OSError:
            continue
        PORT = port
        break
    if SERVER is None:
        raise RuntimeError("no free port in 7100-7199")
    fanlib.PORT = PORT
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()


def tearDownModule():
    fanlib.FALLBACK.stop()
    if SERVER is not None:
        SERVER.shutdown()
        SERVER.server_close()
    shutil.rmtree(TMP, ignore_errors=True)


def request(method, path, body=None, headers=None, host=None):
    """(status, {lowercased header: value}, body bytes). JSON bodies get application/json."""
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        hdrs = {"Host": host if host is not None else f"127.0.0.1:{PORT}"}
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(data))
        hdrs.update(headers or {})
        for k, v in hdrs.items():
            if v is not None:
                conn.putheader(k, v)
        conn.endheaders(data)
        r = conn.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        conn.close()


def get_json(path):
    status, _, body = request("GET", path)
    assert status == 200, (path, status, body)
    return json.loads(body)


def post(path, body, headers=None):
    status, _, raw = request("POST", path, body=body, headers=headers)
    return status, json.loads(raw) if raw else None


def raw_exchange(data, timeout=5.0):
    """Send raw bytes, return (status code, raw response). For malformed requests."""
    with socket.create_connection(("127.0.0.1", PORT), timeout=timeout) as s:
        s.sendall(data)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    return int(buf.split(b" ", 2)[1]), buf


class BaseCase(unittest.TestCase):
    def setUp(self):
        write(ENV["FANCTL_FAN_PROC"], PROC_CONTROL)
        daemon_up()
        rm(SOCK)
        rm(ENV["FANCTL_AUTOSTART_FILE"])
        rm(ENV["FANCTL_CONFIG_FILE"])
        fanlib.reload_settings()
        rm(ENV["FANCTL_SETTINGS_FILE"])
        fanlib._rt_set(vrm_desired=False, tdp_locked=False, tdp_lock_paused_thermal=False,
                       startup_tdp_skipped=None, hold_owned=False, owned_set_at=None)
        self._smu = fanlib.SMU
        fanlib.SMU = fanlib.SmuCache()

    def tearDown(self):
        fanlib.FALLBACK.stop()
        fanlib.SMU = self._smu
        rm(SOCK)


# ─────────────────────────────────────────────────────────────────────────────
# §14 request gates and headers
# ─────────────────────────────────────────────────────────────────────────────

class GateTests(BaseCase):

    def test_host_gate_421_with_empty_body(self):
        for host in ("evil.example:%d" % PORT, "127.0.0.1:1", "127.0.0.1", "localhost",
                     "127.0.0.2:%d" % PORT, "[::1]:%d" % PORT, ""):
            status, _, body = request("GET", "/api/status", host=host)
            self.assertEqual((status, body), (421, b""), host)
        status, _, body = request("POST", "/api/fan/resume", body={}, host="rebind.example:%d" % PORT)
        self.assertEqual((status, body), (421, b""))
        status, _, _ = request("OPTIONS", "/api/status", host="evil.example")
        self.assertEqual(status, 421)
        # No Host header at all (HTTP/1.0 style).
        status, _ = raw_exchange(b"GET /api/status HTTP/1.0\r\n\r\n")
        self.assertEqual(status, 421)
        for host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"LOCALHOST:{PORT}"):
            self.assertEqual(request("GET", "/api/settings", host=host)[0], 200, host)

    def test_content_type_gate_415(self):
        for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""):
            status, _, body = request("POST", "/api/settings", body={}, headers={"Content-Type": ctype})
            self.assertEqual(status, 415, ctype)
            self.assertFalse(json.loads(body)["success"])
        status, _, _ = request("POST", "/api/settings", body={},
                               headers={"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(status, 200)

    def test_origin_gate_403(self):
        for origin in ("http://evil.example", "null", f"http://127.0.0.1:{PORT + 1}",
                       f"https://127.0.0.1:{PORT}", f"http://localhost:{PORT}.evil.example"):
            status, body = post("/api/settings", {}, headers={"Origin": origin})
            self.assertEqual(status, 403, origin)
            self.assertFalse(body["success"])
        for origin in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            self.assertEqual(post("/api/settings", {}, headers={"Origin": origin})[0], 200, origin)

    def test_sec_fetch_site_gate_403(self):
        for sfs in ("cross-site", "same-site", "bogus"):
            self.assertEqual(post("/api/settings", {}, headers={"Sec-Fetch-Site": sfs})[0], 403, sfs)
        for sfs in ("same-origin", "none"):
            self.assertEqual(post("/api/settings", {}, headers={"Sec-Fetch-Site": sfs})[0], 200, sfs)

    def test_content_length_400_and_413_before_reading(self):
        base = f"POST /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\nContent-Type: application/json\r\n"
        for cl in ("abc", "-1", "1e3"):
            status, _ = raw_exchange((base + f"Content-Length: {cl}\r\n\r\n").encode())
            self.assertEqual(status, 400, cl)
        # 413 must come back without the server waiting for the (unsent) body:
        # reading it would stall until Handler.timeout (10 s).
        t0 = time.monotonic()
        status, _ = raw_exchange((base + "Content-Length: 65537\r\n\r\n").encode(), timeout=5)
        self.assertEqual(status, 413)
        self.assertLess(time.monotonic() - t0, 3)
        status, _ = raw_exchange((base + "Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n").encode())
        self.assertEqual(status, 400)
        self.assertEqual(post("/api/settings", b"{not json")[0], 400)
        self.assertEqual(post("/api/settings", [1, 2])[0], 400)
        self.assertEqual(post("/api/settings", b"\xff\xfe")[0], 400)
        self.assertEqual(post("/api/settings", b"")[0], 200)          # empty body = {}

    def test_options_403_and_never_cors(self):
        status, headers, _ = request("OPTIONS", "/api/fan/hold",
                                     headers={"Origin": "http://evil.example",
                                              "Access-Control-Request-Method": "POST"})
        self.assertEqual(status, 403)
        for method, path, body in (("OPTIONS", "/api/status", None), ("GET", "/api/status", None),
                                   ("POST", "/api/settings", {}), ("GET", "/", None)):
            _, headers, _ = request(method, path, body=body, headers={"Origin": f"http://127.0.0.1:{PORT}"})
            self.assertFalse([h for h in headers if h.startswith("access-control-")], (method, path))

    def test_security_headers_on_every_response(self):
        cases = [("GET", "/", None, None), ("GET", "/index.html", None, None),
                 ("GET", "/api/status", None, None), ("GET", "/api/log", None, None),
                 ("GET", "/api/nope", None, None), ("GET", "/nope", None, None),
                 ("POST", "/api/settings", {}, None), ("OPTIONS", "/", None, None),
                 ("POST", "/api/settings", {}, {"Content-Type": "text/plain"}),
                 ("GET", "/api/status", None, "evil.example")]
        for method, path, body, extra in cases:
            host = extra if isinstance(extra, str) else None
            hdrs = extra if isinstance(extra, dict) else None
            _, headers, _ = request(method, path, body=body, headers=hdrs, host=host)
            self.assertEqual(headers.get("x-content-type-options"), "nosniff", (method, path))
            self.assertEqual(headers.get("cache-control"), "no-store", (method, path))
        status, headers, body = request("GET", "/?embedded=1")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-security-policy"), CSP)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertIn(b"test dashboard", body)

    def test_gate_order(self):
        """§14 order: Host → Content-Type → Origin → Sec-Fetch-Site → Content-Length."""
        base = "POST /api/settings HTTP/1.1\r\nConnection: close\r\n"
        cases = [
            # (headers, expected status): each case breaks two gates; the earlier one must win.
            (f"Host: evil.example:{PORT}\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n", 421),
            (f"Host: 127.0.0.1:{PORT}\r\nContent-Type: text/plain\r\nOrigin: http://evil.example\r\n"
             "Content-Length: 2\r\n", 415),
            (f"Host: 127.0.0.1:{PORT}\r\nContent-Type: application/json\r\nOrigin: http://evil.example\r\n"
             "Sec-Fetch-Site: cross-site\r\nContent-Length: abc\r\n", 403),
            (f"Host: 127.0.0.1:{PORT}\r\nContent-Type: application/json\r\nSec-Fetch-Site: cross-site\r\n"
             "Content-Length: abc\r\n", 403),
            (f"Host: 127.0.0.1:{PORT}\r\nContent-Type: application/json\r\nContent-Length: 99999999\r\n", 413),
        ]
        for hdrs, want in cases:
            status, raw = raw_exchange((base + hdrs + "\r\n{}").encode())
            self.assertEqual(status, want, hdrs)
        # The Origin rejection names the origin problem, not the Sec-Fetch-Site one.
        status, body = post("/api/settings", {}, headers={"Origin": "http://evil.example",
                                                          "Sec-Fetch-Site": "cross-site"})
        self.assertEqual((status, body["error"]), (403, "Cross-origin requests are not accepted."))

    def test_head_and_non_standard_json(self):
        status, headers, body = request("HEAD", "/")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers.get("content-security-policy"), CSP)
        self.assertGreater(int(headers["content-length"]), 0)
        self.assertEqual(request("HEAD", "/api/status")[0], 200)
        # NaN / Infinity are not JSON: a body carrying them is a 400, never a value.
        for raw in (b'{"tdp": NaN}', b'{"level": "3", "seconds": Infinity}', b'{"x": -Infinity}'):
            status, r = post("/api/tdp/set", raw)
            self.assertEqual(status, 400, raw)
            self.assertEqual(r["error"], "The request body is not valid JSON.")
        self.assertIsNone(fanlib.get_settings()["tdp_requested"])

    def test_handler_timeout_and_unknown_paths(self):
        self.assertEqual(fanlib.Handler.timeout, 10)
        status, _, body = request("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertFalse(json.loads(body)["success"])
        self.assertEqual(post("/api/nope", {})[0], 404)
        self.assertEqual(request("GET", "/etc/passwd")[0], 404)
        self.assertEqual(request("PUT", "/api/settings", body={})[0], 405)


# ─────────────────────────────────────────────────────────────────────────────
# §15 status object
# ─────────────────────────────────────────────────────────────────────────────

def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class StatusTests(BaseCase):

    def assert_opt(self, v, types, key):
        self.assertTrue(v is None or (isinstance(v, types) and not isinstance(v, bool)), (key, v))

    def test_every_field_with_the_right_type(self):
        daemon_up(reason="curve")
        s = get_json("/api/status")
        self.assertEqual(sorted(set(STATUS_KEYS) - set(s)), [])
        self.assertEqual(s["version"], "2.0.0")
        self.assertTrue(is_num(s["ts"]) and abs(s["ts"] - time.time()) < 5)
        self.assertEqual(sorted(s["temps"]), ["cpu", "igpu", "nvme", "wifi"])
        for k, v in s["temps"].items():
            self.assert_opt(v, int, "temps." + k)                # §15: ints or null
        self.assertEqual(s["temp_c"], 71.4)                      # state.temp_raw while active
        for k in ("fan_rpm", "fan1_rpm", "speed", "cpu_mhz_avg", "gpu_mhz", "gpu_busy",
                  "battery_pct", "boost", "tdp_requested", "startup_tdp_skipped"):
            self.assert_opt(s[k], int if k != "startup_tdp_skipped" else str, k)
        self.assertEqual(s["fan1_rpm"], s["fan_rpm"])
        self.assertEqual(s["level"], "7")
        self.assertEqual(s["speed"], 4100)
        for k in ("fan_control_available", "fan_enabled", "on_ac", "daemon_active", "manual_fallback",
                  "manual_critical", "tdp_locked", "tdp_lock_paused_thermal", "apply_tdp_at_startup",
                  "vrm_unlocked", "vrm_desired", "freeze_config_warning"):
            self.assertIsInstance(s[k], bool, k)
        self.assertIs(s["fan_control_available"], True)
        self.assertIs(s["fan_enabled"], s["fan_control_available"])
        self.assertEqual(s["fan_status"], "enabled")
        for k in ("governor", "battery_status"):
            self.assert_opt(s[k], str, k)
        for k in ("loadavg1", "battery_watts", "state_age_s", "config_mtime"):
            self.assert_opt(s[k], (int, float), k)
        self.assertTrue(s["daemon_active"])
        self.assertIn(s["daemon_enabled"], (None, True, False))  # systemctl skipped in dry run → null
        self.assertIsInstance(s["state"], dict)
        self.assertEqual(s["state"]["pid"], 4242)
        self.assertLess(s["state_age_s"], 3)
        self.assertEqual(s["mode"], "curve")
        self.assertEqual(sorted(s["smu"]), sorted(SMU_KEYS + ["error"]))
        self.assertIs(s["smu"]["ok"], False)                     # dry run never reads the SMU
        self.assertIs(s["smu"]["stale"], False)
        for k in ("tdp", "power", "edc", "edc_limit", "tdc", "tdc_limit", "thm_limit"):
            self.assertEqual(s[k], s["smu"][k], k)                # flattened copies
            self.assertIsNone(s[k], k)                            # never a synthesized 22 W
        self.assertEqual((s["tdp_max"], s["tdp_max_vrm_unlocked"]), (35, 30))
        self.assertEqual(s["critical_temp"], 90)
        self.assertEqual(s["hold_default_seconds"], 900)
        self.assertIn(s["mode"], MODES)

    def test_mode_for_each_reason(self):
        table = {"curve": "curve", "critical": "critical", "override": "hold",
                 "override_suspended_hot": "hold_suspended", "sensor_lost": "sensor_lost",
                 "something_new": "curve"}
        for reason, want in table.items():
            self.assertEqual(fanlib.mode(True, "7", {"reason": reason}), want, reason)
            daemon_up(reason=reason)
            self.assertEqual(get_json("/api/status")["mode"], want, reason)
        self.assertEqual(fanlib.mode(False, "auto", None), "firmware")
        self.assertEqual(fanlib.mode(False, None, None), "firmware")
        self.assertEqual(fanlib.mode(False, "7", {"reason": "override"}), "manual_unprotected")
        self.assertEqual(fanlib.mode(False, "0", None), "manual_unprotected")
        daemon_down()
        write(ENV["FANCTL_FAN_PROC"], PROC_NO_CONTROL)             # level auto
        self.assertEqual(get_json("/api/status")["mode"], "firmware")
        write(ENV["FANCTL_FAN_PROC"], PROC_CONTROL)                # level 7, no daemon
        self.assertEqual(get_json("/api/status")["mode"], "manual_unprotected")

    def test_daemon_active_by_ts_age(self):
        now = time.time()
        self.assertTrue(fanlib.daemon_active({"ts": now - 4.8, "sample_interval": 1}))
        self.assertFalse(fanlib.daemon_active({"ts": now - 5.2, "sample_interval": 1}))
        self.assertTrue(fanlib.daemon_active({"ts": now - 7.8, "sample_interval": 2}))
        self.assertFalse(fanlib.daemon_active({"ts": now - 8.2, "sample_interval": 2}))
        self.assertFalse(fanlib.daemon_active(None))
        self.assertFalse(fanlib.daemon_active({"ts": "yesterday"}))
        daemon_up(ts=now - 6.0, reason="override")
        s = get_json("/api/status")
        self.assertFalse(s["daemon_active"])
        self.assertIsNone(s["state"])                             # a stale state is not handed out
        self.assertTrue(5.5 < s["state_age_s"] < 8)
        self.assertEqual(s["mode"], "manual_unprotected")         # /proc says level 7
        self.assertIsInstance(s["temp_c"], (int, type(None)))     # max(cpu, igpu) once the daemon is gone
        write(STATE, "{truncated")
        s = get_json("/api/status")
        self.assertFalse(s["daemon_active"])
        self.assertIsNone(s["state_age_s"])
        daemon_down()
        s = get_json("/api/status")
        self.assertEqual((s["daemon_active"], s["state"], s["state_age_s"]), (False, None, None))
        # A ts far in the future (wall clock stepped back after the last write)
        # is stale too; a slightly-future one (write/read race) is live.
        self.assertTrue(fanlib.daemon_active({"ts": now + 1.0, "sample_interval": 1}))
        self.assertFalse(fanlib.daemon_active({"ts": now + 60.0, "sample_interval": 1}))
        self.assertFalse(fanlib.daemon_active({"ts": True, "sample_interval": 1}))     # bool is not a number
        self.assertTrue(fanlib.daemon_active({"ts": now - 4.0, "sample_interval": "x"}))  # bad si → 1

    def test_non_standard_json_in_state_does_not_break_status(self):
        # Python's json writes NaN for a float('nan'); one in state.json must
        # not turn /api/status into a 500 (the strict encoder refuses NaN).
        text = json.dumps(fresh_state(temp_fast=float("nan"), temp_slow=float("inf"), reason="override",
                                      override=override_obj("4")))
        self.assertIn("NaN", text)
        write(STATE, text)
        status, _, body = request("GET", "/api/status")
        self.assertEqual(status, 200)
        s = json.loads(body)
        self.assertTrue(s["daemon_active"])
        self.assertEqual(s["mode"], "hold")
        self.assertIsNone(s["state"]["temp_fast"])
        self.assertIsNone(s["state"]["temp_slow"])
        self.assertEqual(s["temp_c"], 71.4)

    def test_read_fan_proc_parser(self):
        cases = {
            PROC_CONTROL: (True, "enabled", "7", 4100),
            PROC_LEVEL0: (True, "disabled", "0", 0),                  # disabled == stopped, still controllable
            PROC_NO_CONTROL: (False, "enabled", "auto", 2900),
            # commands: lines that do not offer `level` are not fan control.
            "status:\t\tenabled\nspeed:\t\t3000\nlevel:\t\tauto\ncommands:\tenable, disable\n"
            "commands:\twatchdog <timeout> (<timeout> is 0 (off), 1-120 (seconds))\n": (False, "enabled", "auto", 3000),
            "status:\t\tdisabled\nspeed:\t\t0\nlevel:\t\t0\n": (False, "disabled", "0", 0),
            "speed:\t\tgarbage\nlevel:\n\n": (False, None, None, None),
        }
        for text, (avail, status_word, level, speed) in cases.items():
            write(ENV["FANCTL_FAN_PROC"], text)
            fp = fanlib.read_fan_proc()
            self.assertEqual((fp["readable"], fp["control_available"], fp["status"], fp["level"], fp["speed"]),
                             (True, avail, status_word, level, speed), text)
        rm(ENV["FANCTL_FAN_PROC"])
        self.assertEqual(fanlib.read_fan_proc(), {"readable": False, "level": None, "speed": None,
                                                  "status": None, "control_available": False})

    def test_fan_control_available_from_proc(self):
        daemon_down()
        write(ENV["FANCTL_FAN_PROC"], PROC_CONTROL)
        s = get_json("/api/status")
        self.assertEqual((s["fan_control_available"], s["fan_enabled"], s["fan_status"]), (True, True, "enabled"))
        # Level 0: the EC status byte reads "disabled" but control is available.
        write(ENV["FANCTL_FAN_PROC"], PROC_LEVEL0)
        s = get_json("/api/status")
        self.assertEqual((s["fan_control_available"], s["fan_enabled"]), (True, True))
        self.assertEqual((s["fan_status"], s["level"], s["speed"]), ("disabled", "0", 0))
        write(ENV["FANCTL_FAN_PROC"], PROC_NO_CONTROL)
        s = get_json("/api/status")
        self.assertEqual((s["fan_control_available"], s["fan_enabled"], s["fan_status"]), (False, False, "enabled"))
        self.assertEqual(s["level"], "auto")
        rm(ENV["FANCTL_FAN_PROC"])
        s = get_json("/api/status")
        self.assertEqual((s["fan_control_available"], s["fan_status"], s["level"], s["speed"]),
                         (False, None, None, None))
        # /proc unreadable while the daemon is live: its published parse is used.
        daemon_up(fan_control_available=True, fan_status="disabled", level_proc="0", rpm=0)
        s = get_json("/api/status")
        self.assertEqual((s["fan_control_available"], s["fan_status"], s["level"]), (True, "disabled", "0"))

    def test_smu_ok_stale_and_vrm_from_hardware(self):
        parsed = fanlib.parse_ryzenadj_info(0, SMU_TABLE)
        self.assertEqual(parsed, {"tdp": 29, "power": 11.9, "power_slow": 9.5, "edc": 40.7,
                                  "edc_limit": 45.0, "tdc": 10.1, "tdc_limit": 35.0, "thm_limit": 95.0})
        with self.assertRaises(RuntimeError):
            fanlib.parse_ryzenadj_info(1, "", "Unable to init ryzenadj")
        with self.assertRaises(RuntimeError):
            fanlib.parse_ryzenadj_info(0, "| PPT VALUE FAST | 3.0 | |\n")

        unlocked = dict(parsed, tdp=30, edc_limit=60.0, tdc_limit=42.0)
        fanlib.SMU.reader = lambda: unlocked
        s = get_json("/api/status")
        self.assertEqual((s["smu"]["ok"], s["smu"]["stale"], s["tdp"], s["edc_limit"]), (True, False, 30, 60.0))
        self.assertIsInstance(s["smu"]["updated_at"], float)
        self.assertIs(s["vrm_unlocked"], True)                    # hardware truth, not session intent
        self.assertIs(s["vrm_desired"], False)
        self.assertIs(s["freeze_config_warning"], True)
        # §15 types with a good reading: tdp an int (comparable with
        # tdp_requested), the other readings floats, flattened copies equal.
        self.assertIs(type(s["smu"]["tdp"]), int)
        for k in ("power", "power_slow", "edc", "edc_limit", "tdc", "tdc_limit", "thm_limit"):
            self.assertIsInstance(s["smu"][k], float, k)
            if k != "power_slow":
                self.assertEqual(s[k], s["smu"][k], k)
        self.assertIsNone(s["smu"]["error"])
        # Below the 30 W line, or with stock EDC, there is no freeze warning.
        fanlib.SMU = fanlib.SmuCache(reader=lambda: dict(unlocked, tdp=29))
        self.assertIs(get_json("/api/status")["freeze_config_warning"], False)
        fanlib.SMU = fanlib.SmuCache(reader=lambda: dict(unlocked, edc_limit=45.0))
        s = get_json("/api/status")
        self.assertEqual((s["vrm_unlocked"], s["freeze_config_warning"]), (False, False))
        fanlib.SMU = fanlib.SmuCache(reader=lambda: unlocked)
        fanlib.SMU.get()

        def boom():
            raise RuntimeError("ryzenadj --info failed: exit 1")
        fanlib.SMU.reader = boom
        s = fanlib.SMU.get(force=True)
        self.assertEqual((s["ok"], s["stale"], s["tdp"], s["edc_limit"]), (False, True, 30, 60.0))
        self.assertIn("exit 1", s["error"])

        fanlib.SMU = fanlib.SmuCache(reader=boom)                 # never had a good reading
        s = get_json("/api/status")
        self.assertEqual((s["smu"]["ok"], s["smu"]["stale"], s["tdp"]), (False, False, None))
        self.assertIs(s["vrm_unlocked"], False)
        self.assertIs(s["freeze_config_warning"], False)

    def test_smu_cadence_follows_visibility(self):
        calls = []
        fanlib.SMU.reader = lambda: calls.append(1) or fanlib.parse_ryzenadj_info(0, SMU_TABLE)
        fanlib.set_visibility(True)
        self.assertEqual(fanlib.SMU.ttl(), 10.0)
        fanlib.SMU.get()
        fanlib.SMU.get()
        self.assertEqual(len(calls), 1)                          # cached inside the TTL
        fanlib.set_visibility(False)
        self.assertEqual(fanlib.SMU.ttl(), 30.0)
        fanlib.SMU._attempt_mono -= 15                           # 15 s later: fresh enough when hidden
        fanlib.SMU.get()
        self.assertEqual(len(calls), 1)
        fanlib.set_visibility(True)
        fanlib.SMU.get()                                         # but stale for a visible window
        self.assertEqual(len(calls), 2)


# ─────────────────────────────────────────────────────────────────────────────
# /api/history, /api/log
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_KEYS = sorted("t cpu igpu ctrl fast slow rpm level ac tdp power".split())


class HistoryTests(BaseCase):

    def setUp(self):
        super().setUp()
        now = self.now = time.time()
        # 40 minutes at 10 s, oldest first, in the §6 row layout. Ages end in
        # 5 so no sample sits exactly on a minutes cutoff.
        samples = [[now - (i + 5), 60.0 + i % 7, 59.5, 58.25, "6" if i % 20 else "7", 3800 + i, i % 3 != 0]
                   for i in range(2400, -1, -10)]
        samples.append(["garbage"])
        events = [[now - 2000, "config", "reload"], [now - 100, "level", "6->7"],
                  [now - 50, "override_start", None], ["bad"]]
        write_json(HISTORY, {"schema": 1, "sample_interval": 1, "samples": samples, "events": events})
        with fanlib._samples_lock:
            fanlib.BACKEND_SAMPLES.clear()
            fanlib.BACKEND_SAMPLES.append({"t": now - 63, "tdp": 25, "power": 11.5, "cpu": 70, "igpu": 66})
            fanlib.BACKEND_SAMPLES.append({"t": now - 33, "tdp": None, "power": None, "cpu": 72, "igpu": 67})

    def tearDown(self):
        with fanlib._samples_lock:
            fanlib.BACKEND_SAMPLES.clear()
        super().tearDown()

    def test_merge_shape_and_minutes_cap(self):
        h = get_json("/api/history")
        self.assertEqual(sorted(h), ["events", "samples"])
        self.assertTrue(h["samples"])
        for smp in h["samples"]:
            self.assertEqual(sorted(smp), SAMPLE_KEYS)
            self.assertGreaterEqual(smp["t"], self.now - 1800 - 1)          # default 30 min
            self.assertIsInstance(smp["level"], str)
            self.assertIsInstance(smp["ac"], bool)
        self.assertEqual(len(h["samples"]), 180)                             # ages 5..1795 s
        self.assertEqual([e["kind"] for e in h["events"]], ["level", "override_start"])
        self.assertEqual(h["events"][1], {"t": h["events"][1]["t"], "kind": "override_start", "detail": ""})

        by_t = {round(self.now - x["t"]): x for x in h["samples"]}
        self.assertEqual((by_t[65]["tdp"], by_t[65]["power"], by_t[65]["cpu"]), (25, 11.5, 70))   # 2 s away
        self.assertEqual((by_t[35]["tdp"], by_t[35]["cpu"], by_t[35]["igpu"]), (None, 72, 67))
        self.assertEqual((by_t[305]["tdp"], by_t[305]["cpu"]), (None, None))  # nothing within 5 s
        self.assertEqual((by_t[25]["cpu"], by_t[45]["cpu"]), (None, None))    # 8 and 12 s away
        self.assertEqual(by_t[65]["ctrl"], 60.0 + 60 % 7)
        self.assertEqual((by_t[65]["fast"], by_t[65]["slow"], by_t[65]["rpm"]), (59.5, 58.25, 3860))
        self.assertEqual((by_t[65]["level"], by_t[75]["level"], by_t[65]["ac"]), ("7", "6", False))

        self.assertTrue(all(x["t"] >= self.now - 301 for x in get_json("/api/history?minutes=5")["samples"]))
        self.assertEqual(len(get_json("/api/history?minutes=5")["samples"]), 30)
        self.assertEqual(len(get_json("/api/history?minutes=999")["samples"]), 180)   # max 30
        self.assertEqual(len(get_json("/api/history?minutes=abc")["samples"]), 180)
        self.assertEqual(len(get_json("/api/history?minutes=0")["samples"]), 6)       # floor of 1 min

    def test_empty_when_daemon_down(self):
        daemon_up(ts=time.time() - 30)
        self.assertEqual(get_json("/api/history"), {"samples": [], "events": []})
        daemon_up()                                                 # live, but no history.json yet
        rm(HISTORY)
        self.assertEqual(get_json("/api/history"), {"samples": [], "events": []})
        write(HISTORY, '{"schema": 1, "samples": "nope", "events": {}}')
        self.assertEqual(get_json("/api/history"), {"samples": [], "events": []})
        daemon_down()
        self.assertEqual(get_json("/api/history"), {"samples": [], "events": []})

    def test_backend_sampler_copies_the_smu_cache_without_refreshing(self):
        calls = []
        fanlib.SMU.reader = lambda: calls.append(1) or fanlib.parse_ryzenadj_info(0, SMU_TABLE)
        fanlib.sample_backend()
        self.assertEqual(calls, [])
        self.assertEqual(fanlib.BACKEND_SAMPLES[-1]["tdp"], None)
        fanlib.SMU.get(force=True)
        fanlib.sample_backend()
        self.assertEqual(len(calls), 1)
        self.assertEqual((fanlib.BACKEND_SAMPLES[-1]["tdp"], fanlib.BACKEND_SAMPLES[-1]["power"]), (29, 11.9))


class LogTests(BaseCase):

    def test_nul_stripping_decoding_and_limits(self):
        log = ENV["FANCTL_LOG_FILE"]
        body = b"".join(b"[2026-09-23 12:00:%02d] line %d\n" % (i % 60, i) for i in range(2500))
        crash = b"\x00" * (1 << 20)                                  # 1 MiB NUL padding, no newlines
        tail = b"[2026-09-23 13:00:00] after crash \xff\xfe bytes\n\x00\x00[2026-09-23 13:00:01] Fan 7 -> disengaged\n"
        write(log, body + crash + tail, mode="wb")
        status, headers, raw = request("GET", "/api/log?lines=2")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/plain"))
        text = raw.decode("utf-8")
        self.assertNotIn("\x00", text)
        lines = text.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("�", lines[0])                            # errors="replace"
        self.assertEqual(lines[1], "[2026-09-23 13:00:01] Fan 7 -> disengaged")
        lines = request("GET", "/api/log")[2].decode().splitlines()
        self.assertEqual(len(lines), 200)                            # default
        self.assertTrue(lines[0].startswith("[2026-09-23 12:"))      # read across the NUL region
        lines = request("GET", "/api/log?lines=5000")[2].decode().splitlines()
        self.assertEqual(len(lines), 2000)                           # max
        self.assertEqual(lines[-1], "[2026-09-23 13:00:01] Fan 7 -> disengaged")
        self.assertEqual(len(request("GET", "/api/log?lines=junk")[2].decode().splitlines()), 200)

    def test_missing_log_is_empty(self):
        rm(ENV["FANCTL_LOG_FILE"])
        self.assertEqual(request("GET", "/api/log")[:1] + (request("GET", "/api/log")[2],), (200, b""))


# ─────────────────────────────────────────────────────────────────────────────
# §9 settings and /api/settings
# ─────────────────────────────────────────────────────────────────────────────

class SettingsTests(BaseCase):

    def read_disk(self):
        with open(ENV["FANCTL_SETTINGS_FILE"]) as f:
            return json.load(f)

    def test_round_trip_and_vrm_never_persisted(self):
        s = get_json("/api/settings")
        self.assertEqual(s["schema"], 1)
        self.assertEqual({k: s[k] for k in ("tdp_requested", "tdp_locked", "apply_tdp_at_startup", "last_clean_exit")},
                         {"tdp_requested": None, "tdp_locked": False, "apply_tdp_at_startup": False,
                          "last_clean_exit": True})
        self.assertEqual(s["ui"], {"hold_default_seconds": 900, "history_range_min": 15,
                                   "log_filter": "all", "view": "overview"})
        self.assertIsNone(s["autostart_enabled"])                   # no autostart entry installed

        status, r = post("/api/settings", {"ui": {"view": "power", "hold_default_seconds": 300, "zoom": 2},
                                           "apply_tdp_at_startup": True, "vrm_unlocked": True,
                                           "tdp_requested": 30, "last_clean_exit": False})
        self.assertEqual(status, 200)
        self.assertTrue(r["success"], r)
        self.assertEqual(sorted(r["ignored"]), ["last_clean_exit", "tdp_requested", "ui.zoom", "vrm_unlocked"])
        disk = self.read_disk()
        self.assertEqual(disk["ui"]["view"], "power")
        self.assertEqual(disk["ui"]["hold_default_seconds"], 300)
        self.assertIs(disk["apply_tdp_at_startup"], True)
        self.assertIsNone(disk["tdp_requested"])                     # not settable here
        self.assertIs(disk["last_clean_exit"], True)
        self.assertFalse([k for k in json.dumps(disk).split('"') if "vrm" in k])
        self.assertEqual(stat.S_IMODE(os.stat(ENV["FANCTL_SETTINGS_FILE"]).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(ENV["FANCTL_SETTINGS_FILE"])).st_mode), 0o700)
        fanlib.reload_settings()                                     # really from disk
        s = get_json("/api/settings")
        self.assertEqual((s["ui"]["view"], s["ui"]["history_range_min"]), ("power", 15))
        self.assertEqual(get_json("/api/status")["hold_default_seconds"], 300)

        # A VRM unlock is session-only: status shows the intent, the file never does.
        post("/api/tdp/set", {"tdp": 20})
        status, r = post("/api/tdp/vrm_unlock", {"unlocked": True})
        self.assertTrue(r["success"], r)
        self.assertIs(get_json("/api/status")["vrm_desired"], True)
        self.assertNotIn("vrm", json.dumps(self.read_disk()))
        self.assertTrue(post("/api/tdp/restore_stock", {})[1]["success"])
        self.assertIs(get_json("/api/status")["vrm_desired"], False)

    def test_invalid_values_change_nothing(self):
        post("/api/settings", {"ui": {"view": "log"}})
        for body in ({"ui": {"hold_default_seconds": -5, "view": "power"}},
                     {"ui": {"hold_default_seconds": 99999}},
                     {"ui": {"history_range_min": 31}},
                     {"ui": {"view": "<script>"}},
                     {"ui": "power"},
                     {"apply_tdp_at_startup": "true"},
                     {"autostart_enabled": 1},
                     {"ui": {"hold_default_seconds": True}}):
            status, r = post("/api/settings", body)
            self.assertEqual(status, 200)
            self.assertFalse(r["success"], body)
            self.assertIsInstance(r["error"], str)
        s = get_json("/api/settings")
        self.assertEqual((s["ui"]["view"], s["ui"]["hold_default_seconds"], s["apply_tdp_at_startup"]),
                         ("log", 900, False))
        self.assertTrue(post("/api/settings", {"ui": {"hold_default_seconds": 0}})[1]["success"])  # until resumed

    def test_autostart_rewrites_only_its_line(self):
        path = ENV["FANCTL_AUTOSTART_FILE"]
        original = ("[Desktop Entry]\nType=Application\nName=Fan Control\n"
                    "Exec=python3 /x/app.py --tray\nX-GNOME-Autostart-enabled=false\nHidden=false\n")
        write(path, original)
        os.chmod(path, 0o644)
        self.assertIs(get_json("/api/settings")["autostart_enabled"], False)
        status, r = post("/api/settings", {"autostart_enabled": True})
        self.assertTrue(r["success"], r)
        self.assertIs(r["settings"]["autostart_enabled"], True)
        with open(path) as f:
            self.assertEqual(f.read(), original.replace("enabled=false", "enabled=true"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        self.assertNotIn("autostart_enabled", fanlib.get_settings())      # never persisted
        rm(path)
        status, r = post("/api/settings", {"autostart_enabled": False})
        self.assertFalse(r["success"])
        self.assertFalse(os.path.exists(path))                     # never created by the backend

    def test_failed_settings_write_keeps_memory_and_leaves_no_temp(self):
        path = ENV["FANCTL_SETTINGS_FILE"]
        d = os.path.dirname(path)
        post("/api/settings", {"ui": {"view": "log"}})                 # creates dir + file
        rm(path)
        os.mkdir(path)                     # the final os.replace() now fails (EISDIR)
        try:
            status, r = post("/api/settings", {"ui": {"view": "power"}})
            self.assertEqual(status, 200)
            self.assertTrue(r["success"])                              # the session still has it
            self.assertEqual(fanlib.get_settings()["ui"]["view"], "power")
            self.assertTrue(os.path.isdir(path))
            self.assertEqual([f for f in os.listdir(d) if f.endswith(".tmp")], [])   # temp cleaned up
        finally:
            os.rmdir(path)

    def test_non_standard_json_in_settings_file(self):
        write(ENV["FANCTL_SETTINGS_FILE"], '{"schema": 1, "tdp_requested": NaN, "apply_tdp_at_startup": true}')
        fanlib.reload_settings()
        s = get_json("/api/settings")
        self.assertEqual((s["tdp_requested"], s["apply_tdp_at_startup"]), (None, True))

    def test_sanitize_falls_back_per_field(self):
        base = fanlib._sanitize_settings({"tdp_requested": 20, "ui": {"view": "log"}})
        out = fanlib._sanitize_settings({"tdp_requested": 99, "tdp_locked": "yes", "vrm_unlocked": True,
                                         "ui": {"view": "power", "history_range_min": 0}}, base=base)
        self.assertEqual(out["tdp_requested"], 20)
        self.assertIs(out["tdp_locked"], False)
        self.assertEqual((out["ui"]["view"], out["ui"]["history_range_min"]), ("power", 15))
        self.assertNotIn("vrm_unlocked", out)


# ─────────────────────────────────────────────────────────────────────────────
# /api/tdp/*
# ─────────────────────────────────────────────────────────────────────────────

class TdpApiTests(BaseCase):

    def tearDown(self):
        write(ENV["FANCTL_ENV_FILE"], "FANCTL_GUI_UID=1000\nFANCTL_TDP_MAX=35\nFANCTL_TDP_MAX_VRM_UNLOCKED=30\n")
        super().tearDown()

    def test_set_range_and_cap_validation(self):
        for bad in (4, 36, 0, -15, 40, "abc", True, 15.5, None, "015"):
            status, r = post("/api/tdp/set", {"tdp": bad})
            self.assertEqual(status, 200)
            self.assertFalse(r["success"], bad)
            self.assertIsInstance(r["error"], str)
        self.assertIsNone(fanlib.get_settings()["tdp_requested"])
        status, r = post("/api/tdp/set", {"tdp": 35})
        self.assertEqual((r["success"], r["tdp_requested"]), (True, 35))
        self.assertEqual(get_json("/api/settings")["tdp_requested"], 35)
        self.assertTrue(post("/api/tdp/set", {"tdp": 5})[1]["success"])
        self.assertTrue(post("/api/tdp/set", {"tdp": "12"})[1]["success"])
        # With the VRM unlocked (session intent) the lower cap applies.
        fanlib._rt_set(vrm_desired=True)
        r = post("/api/tdp/set", {"tdp": 31})[1]
        self.assertFalse(r["success"])
        self.assertIn("30 W", r["error"])
        self.assertTrue(post("/api/tdp/set", {"tdp": 30})[1]["success"])

    def test_caps_follow_daemon_env(self):
        write(ENV["FANCTL_ENV_FILE"], 'FANCTL_TDP_MAX="20"\nexport FANCTL_TDP_MAX_VRM_UNLOCKED=25\n')
        s = get_json("/api/status")
        self.assertEqual((s["tdp_max"], s["tdp_max_vrm_unlocked"]), (20, 20))   # unlocked ≤ general
        self.assertFalse(post("/api/tdp/set", {"tdp": 21})[1]["success"])
        self.assertTrue(post("/api/tdp/set", {"tdp": 20})[1]["success"])
        write(ENV["FANCTL_ENV_FILE"], "FANCTL_TDP_MAX=035\nFANCTL_TDP_MAX_VRM_UNLOCKED=abc\n")
        self.assertEqual(fanlib.tdp_caps(), {"tdp_max": 35, "tdp_max_vrm_unlocked": 30})  # like the wrapper
        rm(ENV["FANCTL_ENV_FILE"])
        self.assertEqual(fanlib.tdp_caps(), {"tdp_max": 35, "tdp_max_vrm_unlocked": 30})

    def test_env_values_parse_like_bash(self):
        # ryzenadj-set-tdp.sh sources daemon.env, so a trailing comment or
        # quoting must yield the value bash would assign (else the UI offers
        # watts the wrapper refuses).
        cases = {"25": "25", "25 # lowered": "25", '"25"': "25", '"25"  # c': "25", "'25'": "25",
                 "'25' # c": "25", '"a \\"b\\""': 'a "b"', '"25': '"25', '"25"x': '"25"x', "": "",
                 "25#x": "25#x"}
        for raw, want in cases.items():
            self.assertEqual(fanlib._unquote_env(raw), want, raw)
        write(ENV["FANCTL_ENV_FILE"], "# written by install.sh\nFANCTL_TDP_MAX=25 # after the freeze\n"
                                      "  export FANCTL_TDP_MAX_VRM_UNLOCKED='15'\nFANCTL_GUI_HOME=\"/home/a b\"\n")
        self.assertEqual(fanlib.tdp_caps(), {"tdp_max": 25, "tdp_max_vrm_unlocked": 15})
        self.assertEqual(fanlib.daemon_env()["FANCTL_GUI_HOME"], "/home/a b")
        self.assertFalse(post("/api/tdp/set", {"tdp": 26})[1]["success"])

    def test_vrm_unlock_rules(self):
        post("/api/tdp/set", {"tdp": 33})
        r = post("/api/tdp/vrm_unlock", {"unlocked": True})[1]
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "Lower the TDP to 30 W or below before unlocking")
        self.assertIs(get_json("/api/status")["vrm_desired"], False)
        self.assertFalse(post("/api/tdp/vrm_unlock", {"unlocked": "yes"})[1]["success"])
        post("/api/tdp/set", {"tdp": 25})
        r = post("/api/tdp/vrm_unlock", {"unlocked": True})[1]
        self.assertEqual((r["success"], r["tdp"]), (True, 25))
        r = post("/api/tdp/vrm_unlock", {"unlocked": False})[1]
        self.assertTrue(r["success"])
        self.assertIs(get_json("/api/status")["vrm_desired"], False)

    def test_relock_to_stock_is_never_blocked_by_an_unknown_tdp(self):
        fanlib._rt_set(vrm_desired=True)           # unlocked earlier; now no request and no SMU reading
        r = post("/api/tdp/vrm_unlock", {"unlocked": False})[1]
        self.assertEqual((r["success"], r["tdp"], r["vrm_desired"]), (True, 15, False))
        self.assertIs(get_json("/api/status")["vrm_desired"], False)

    def test_vrm_unlock_without_request_needs_a_good_smu_reading(self):
        self.assertFalse(post("/api/tdp/vrm_unlock", {"unlocked": True})[1]["success"])    # smu not ok
        fanlib.SMU.reader = lambda: fanlib.parse_ryzenadj_info(0, SMU_TABLE)
        r = post("/api/tdp/vrm_unlock", {"unlocked": True})[1]
        self.assertEqual((r["success"], r["tdp"]), (True, 29))

    def test_lock_adopts_the_smu_value_only_when_ok(self):
        r = post("/api/tdp/lock", {"locked": True})[1]
        self.assertFalse(r["success"])                              # no request, SMU unavailable
        self.assertIs(fanlib.get_settings()["tdp_locked"], False)
        fanlib.SMU.reader = lambda: fanlib.parse_ryzenadj_info(0, SMU_TABLE)
        r = post("/api/tdp/lock", {"locked": True})[1]
        self.assertEqual((r["success"], r["tdp_locked"], r["tdp_requested"]), (True, True, 29))
        s = get_json("/api/status")
        self.assertEqual((s["tdp_locked"], s["tdp_requested"]), (True, 29))
        self.assertIs(fanlib.get_settings()["tdp_locked"], True)
        self.assertTrue(post("/api/tdp/lock", {"locked": False})[1]["success"])
        self.assertIs(get_json("/api/status")["tdp_locked"], False)
        self.assertFalse(post("/api/tdp/lock", {"locked": 1})[1]["success"])

    def test_restore_stock(self):
        fanlib._rt_set(vrm_desired=True)
        r = post("/api/tdp/restore_stock", {})[1]
        self.assertEqual((r["success"], r["tdp"]), (True, 15))       # "tdp_requested or 15"
        self.assertIs(get_json("/api/status")["vrm_desired"], False)
        post("/api/tdp/set", {"tdp": 22})
        self.assertEqual(post("/api/tdp/restore_stock", {})[1]["tdp"], 22)


# ─────────────────────────────────────────────────────────────────────────────
# /api/fan/*: socket daemon vs daemon-less fallback
# ─────────────────────────────────────────────────────────────────────────────

class FanHoldTests(BaseCase):

    def setUp(self):
        super().setUp()
        self.fake = None
        self.temp = mock.patch.object(fanlib, "read_control_temp_raw", return_value=45.0)
        self.temp.start()

    def tearDown(self):
        self.temp.stop()
        if self.fake:
            self.fake.close()
        super().tearDown()

    def test_hold_routed_to_the_socket_daemon(self):
        self.fake = FakeDaemon()
        status, r = post("/api/fan/hold", {"level": "3", "seconds": 600})
        self.assertEqual(status, 200)
        self.assertEqual((r["success"], r["error"], r["fallback"]), (True, None, False))
        self.assertEqual(r["state"]["override"]["level"], "3")
        self.assertEqual(self.fake.requests, [{"cmd": "hold", "level": "3", "seconds": 600}])
        self.assertFalse(fanlib.FALLBACK.active)
        self.assertTrue(fanlib._rt("hold_owned"))

        post("/api/fan/hold", {"level": 7, "seconds": "0"})           # int level, digit-string seconds
        self.assertEqual(self.fake.requests[-1], {"cmd": "hold", "level": "7", "seconds": 0})
        post("/api/settings", {"ui": {"hold_default_seconds": 300}})
        post("/api/fan/set", {"level": "disengaged"})                 # legacy alias
        self.assertEqual(self.fake.requests[-1], {"cmd": "hold", "level": "disengaged", "seconds": 300})
        post("/api/fan/hold", {"level": "5"})                          # seconds omitted → default
        self.assertEqual(self.fake.requests[-1]["seconds"], 300)

        r = post("/api/fan/resume", {})[1]
        self.assertEqual((r["success"], r["fallback"]), (True, False))
        self.assertEqual(self.fake.requests[-1], {"cmd": "resume"})
        self.assertFalse(fanlib._rt("hold_owned"))
        self.assertLess(fanlib.last_resume_age(), 5)

        r = post("/api/alert/test", {})[1]
        self.assertEqual((r["success"], r["fallback"]), (True, False))
        self.assertEqual(self.fake.requests[-1], {"cmd": "test_alert"})

    def test_daemon_errors_pass_through(self):
        self.fake = FakeDaemon()
        self.fake.replies["hold"] = {"ok": False, "error": "too_hot_for_fan_off",
                                     "message": "Fan off is refused at 61 °C."}
        r = post("/api/fan/hold", {"level": "0", "seconds": 60})[1]
        self.assertEqual((r["success"], r["error"], r["code"], r["fallback"]),
                         (False, "Fan off is refused at 61 °C.", "too_hot_for_fan_off", False))
        self.fake.replies["hold"] = {"ok": False, "error": "fan_control_unavailable", "message": "No fan_control=1."}
        self.assertEqual(post("/api/fan/hold", {"level": "4"})[1]["code"], "fan_control_unavailable")

    def test_invalid_hold_requests(self):
        self.fake = FakeDaemon()
        for body in ({"level": "8"}, {"level": "max"}, {"level": None}, {"level": True},
                     {"level": "3", "seconds": -5}, {"level": "3", "seconds": "5m"},
                     {"level": "3", "seconds": 1.5}):
            r = post("/api/fan/hold", body)[1]
            self.assertFalse(r["success"], body)
        self.assertEqual(self.fake.requests, [])

    def test_stalled_daemon_never_falls_back(self):
        # A daemon that is there but does not answer must not be fought with
        # direct writes, even when its state.json has already gone stale.
        daemon_up(ts=time.time() - 60)
        stalled = {"ok": False, "error": "timeout", "message": "The daemon did not answer within 3 s."}
        with mock.patch.object(fanlib, "daemon_request", return_value=stalled) as req:
            r = post("/api/fan/hold", {"level": "2", "seconds": 60})[1]
            self.assertEqual((r["success"], r["fallback"], r["code"], r["error"]),
                             (False, False, "timeout", stalled["message"]))
            r = post("/api/fan/resume", {})[1]
            self.assertEqual((r["success"], r["fallback"]), (False, False))
            self.assertEqual(req.call_count, 2)
        self.assertFalse(fanlib.FALLBACK.active)

    def test_socket_client_error_replies(self):
        """daemon_request(): no listener → None (fallback allowed); anything else → an error dict."""
        self.assertIsNone(fanlib.daemon_request({"cmd": "status"}))           # no socket file
        write(SOCK, "not a socket")
        self.assertIsNone(fanlib.daemon_request({"cmd": "status"}))           # connection refused
        rm(SOCK)

        def serve(behaviour):
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(SOCK)
            srv.listen(1)

            def run():
                conn, _ = srv.accept()
                with conn:
                    conn.recv(4096)
                    behaviour(conn)
            t = threading.Thread(target=run, daemon=True)
            t.start()
            return srv, t

        for behaviour, code in ((lambda c: time.sleep(1.0), "timeout"),          # never answers
                                (lambda c: None, "empty_reply"),                   # peer uid refused: silent close
                                (lambda c: c.sendall(b"garbage\n"), "bad_reply"),
                                (lambda c: c.sendall(b"[1, 2]\n"), "bad_reply")):
            srv, t = serve(behaviour)
            try:
                r = fanlib.daemon_request({"cmd": "status"}, timeout=0.3)
                self.assertEqual((r["ok"], r["error"]), (False, code), code)
                self.assertIsInstance(r["message"], str)
            finally:
                t.join(timeout=3)
                srv.close()
                rm(SOCK)

    def test_socket_missing_while_daemon_active_never_falls_back(self):
        r = post("/api/fan/hold", {"level": "3", "seconds": 60})[1]
        self.assertEqual((r["success"], r["fallback"], r["code"]), (False, False, "socket_unreachable"))
        self.assertFalse(fanlib.FALLBACK.active)

    def test_hold_falls_back_without_daemon(self):
        daemon_down()
        r = post("/api/fan/hold", {"level": "4", "seconds": 600})[1]
        self.assertEqual((r["success"], r["error"], r["fallback"], r["seconds"]), (True, None, True, 600))
        self.assertTrue(fanlib.FALLBACK.active)
        s = get_json("/api/status")
        self.assertEqual((s["manual_fallback"], s["manual_critical"], s["manual_fallback_level"]), (True, False, "4"))
        self.assertTrue(595 <= s["manual_fallback_remaining_s"] <= 600)
        self.assertFalse(s["daemon_active"])
        r = post("/api/fan/hold", {"level": "3", "seconds": 0})[1]    # 0 → override_max_seconds
        self.assertEqual((r["success"], r["seconds"]), (True, 7200))
        r = post("/api/fan/resume", {})[1]
        self.assertEqual((r["success"], r["fallback"]), (True, True))
        self.assertFalse(fanlib.FALLBACK.active)
        self.assertFalse(get_json("/api/status")["manual_fallback"])
        r = post("/api/fan/hold", {"level": "auto", "seconds": 60})[1]   # firmware auto = resume
        self.assertEqual((r["success"], r["fallback"]), (True, True))
        self.assertFalse(fanlib.FALLBACK.active)

    def test_fallback_rules(self):
        daemon_down()
        write(ENV["FANCTL_FAN_PROC"], PROC_NO_CONTROL)
        r = post("/api/fan/hold", {"level": "4"})[1]
        self.assertEqual((r["success"], r["code"]), (False, "fan_control_unavailable"))
        # status: disabled only means the fan is stopped at level 0: never a reason to refuse.
        write(ENV["FANCTL_FAN_PROC"], PROC_LEVEL0)
        self.assertTrue(post("/api/fan/hold", {"level": "4"})[1]["success"])
        with mock.patch.object(fanlib, "read_control_temp_raw", return_value=60.0):
            r = post("/api/fan/hold", {"level": "0"})[1]
            self.assertEqual((r["success"], r["code"]), (False, "too_hot_for_fan_off"))
        with mock.patch.object(fanlib, "read_control_temp_raw", return_value=None):
            self.assertFalse(post("/api/fan/hold", {"level": "0"})[1]["success"])   # unknown counts as hot
        self.assertTrue(post("/api/fan/hold", {"level": "0"})[1]["success"])        # 45 °C
        with mock.patch.object(fanlib, "read_control_temp_raw", return_value=91.0):
            self.assertEqual(post("/api/fan/hold", {"level": "5"})[1]["code"], "critical")
            self.assertTrue(post("/api/fan/hold", {"level": "disengaged"})[1]["success"])
        write(ENV["FANCTL_CONFIG_FILE"], json.dumps({"override_max_seconds": 600}))
        try:
            self.assertEqual(post("/api/fan/hold", {"level": "3", "seconds": 900})[1]["seconds"], 600)
        finally:
            rm(ENV["FANCTL_CONFIG_FILE"])


class KeepAliveTickTests(unittest.TestCase):
    """ManualFallback.tick() driven with a fake clock, temperature and writer."""

    def setUp(self):
        self.writes = []
        self.temp = [50.0]
        self.alive = [False]
        patches = [
            mock.patch.object(fanlib, "_fan_set_direct", side_effect=lambda l, w: self.writes.append((l, w)) or (True, None)),
            mock.patch.object(fanlib, "read_control_temp_raw", side_effect=lambda: self.temp[0]),
            mock.patch.object(fanlib, "daemon_active", side_effect=lambda *a: self.alive[0]),
            mock.patch.object(fanlib, "critical_temp_now", return_value=90),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.fb = fanlib.ManualFallback()
        self.fb.level, self.fb.until_mono = "3", 1000.0
        self.fb._last_write, self.fb._written_level = 0.0, "3"
        self.ev = threading.Event()

    def run_until(self, t_end, step=2.0, t0=None):
        t = self.t = (self.t if t0 is None else t0)
        reasons = []
        while t <= t_end:
            r = self.fb.tick(t, self.ev)
            reasons.append(r)
            if r:
                break
            t += step
        self.t = t
        return [r for r in reasons if r]

    def test_level_rewritten_every_30s_with_watchdog_120(self):
        self.t = 0.0
        self.run_until(64)
        self.assertEqual(self.writes, [("3", 120), ("3", 120)])     # at 30 and 60 s

    def test_critical_forces_disengaged_then_firmware_after_cooling(self):
        self.t = 0.0
        self.temp[0] = 92.0
        self.fb.tick(10.0, self.ev)
        self.assertTrue(self.fb.manual_critical)
        self.assertEqual(self.writes[-1], ("disengaged", 120))
        self.temp[0] = 80.0                     # below 90 - 8, but the 60 s exit hold is not over
        self.assertEqual(self.run_until(60, t0=12.0), [])
        self.assertTrue(self.fb.manual_critical)
        self.assertIn(("disengaged", 120), self.writes[1:])          # refreshed while held
        self.temp[0] = 85.0                     # back above the exit threshold: cooling restarts
        self.fb.tick(62.0, self.ev)
        self.temp[0] = 80.0
        self.assertEqual(self.run_until(200, t0=64.0), ["critical_cleared"])
        self.assertGreaterEqual(self.t, 74.0)                          # 10 s of confirmed cooling
        self.assertEqual(self.writes[-1], ("auto", 0))

    def test_expiry_is_deferred_during_critical(self):
        self.fb.until_mono = 20.0
        self.temp[0] = 95.0
        self.fb.tick(10.0, self.ev)
        self.assertIsNone(self.fb.tick(30.0, self.ev))                 # expired, but still critical
        self.assertEqual(self.writes[-1][0], "disengaged")

    def test_sensor_loss_after_three_misses(self):
        self.temp[0] = None
        self.assertIsNone(self.fb.tick(2.0, self.ev))
        self.assertIsNone(self.fb.tick(4.0, self.ev))
        self.assertEqual(self.fb.tick(6.0, self.ev), "sensor_lost")
        self.assertEqual(self.writes, [("auto", 0)])

    def test_expiry_hands_the_fan_to_firmware(self):
        self.assertEqual(self.fb.tick(1000.0, self.ev), "expired")
        self.assertEqual(self.writes, [("auto", 0)])

    def test_daemon_return_ends_without_writing(self):
        self.alive[0] = True
        self.assertEqual(self.fb.tick(40.0, self.ev), "daemon")
        self.assertEqual(self.writes, [])

    def test_no_write_after_stop(self):
        self.ev.set()
        self.fb.tick(40.0, self.ev)
        self.assertEqual(self.writes, [])


# ─────────────────────────────────────────────────────────────────────────────
# TDP lock loop decisions and the §9 startup rule
# ─────────────────────────────────────────────────────────────────────────────

CAPS = {"tdp_max": 35, "tdp_max_vrm_unlocked": 30}


def smu(tdp=25, edc_limit=45.0, ok=True):
    return {"ok": ok, "tdp": tdp, "edc_limit": edc_limit}


class TdpLockTests(unittest.TestCase):

    def setUp(self):
        self.lock = fanlib.TdpLock()

    def decide(self, now=100.0, locked=True, requested=25, vrm=False, reading=None, temp=60.0, crit=90):
        return self.lock.decide(now, locked, requested, vrm, reading or smu(), temp, crit, CAPS)

    def test_no_drift_no_write(self):
        self.assertEqual(self.decide()[0], "ok")

    def test_drift_reapplies_the_request_not_the_smu_value(self):
        action, reasons, _ = self.decide(reading=smu(tdp=15))
        self.assertEqual(action, "apply")
        self.assertEqual(reasons, ["drift 15->25 W"])
        self.assertEqual(self.decide(reading=smu(tdp=25.4))[0], "ok")   # < 1 W is not drift

    def test_floor_of_one_write_per_20s(self):
        self.assertEqual(self.decide(now=100.0, reading=smu(tdp=15))[0], "apply")
        self.assertEqual(self.decide(now=115.0, reading=smu(tdp=15))[0], "floor")
        self.assertEqual(self.decide(now=120.5, reading=smu(tdp=15))[0], "apply")

    def test_thermal_pause_at_critical_minus_3(self):
        self.assertEqual(self.decide(reading=smu(tdp=15), temp=86.9)[0], "apply")
        action, _, paused = self.decide(now=200, reading=smu(tdp=15), temp=87.0)
        self.assertEqual((action, paused), ("paused", True))
        self.assertEqual(self.decide(now=300, reading=smu(tdp=15), temp=None)[0], "paused")   # unknown = hot
        self.assertEqual(self.decide(now=400, temp=88.0)[2], True)      # flag even with nothing to do

    def test_stale_smu_is_never_acted_on(self):
        self.assertEqual(self.decide(reading=smu(tdp=15, ok=False))[0], "wait_smu")
        self.assertEqual(self.decide(reading={"ok": True, "tdp": None})[0], "wait_smu")

    def test_vrm_intent_vs_hardware(self):
        action, reasons, _ = self.decide(reading=smu(edc_limit=60.0), vrm=False)
        self.assertEqual((action, reasons), ("apply", ["vrm stock expected"]))
        self.assertEqual(self.decide(now=200, reading=smu(edc_limit=45.0), vrm=True)[1], ["vrm unlocked expected"])
        self.assertEqual(self.decide(now=300, reading=smu(edc_limit=None), vrm=True)[0], "ok")  # unknown ≠ mismatch

    def test_events_are_latched_until_applied(self):
        self.lock.observe(1000.0, 50.0, True)
        self.lock.observe(1600.0, 55.0, True)                        # 595 s of wall time in 5 s of mono
        self.assertEqual(self.lock.pending, {"suspend/resume"})
        self.lock.observe(1605.0, 60.0, False)
        self.assertEqual(self.lock.pending, {"suspend/resume", "power source changed"})
        self.assertEqual(self.decide(now=100, temp=88.0)[0], "paused")
        action, reasons, _ = self.decide(now=101)
        self.assertEqual((action, reasons), ("apply", ["power source changed", "suspend/resume"]))
        self.lock.applied(False)
        self.assertEqual(self.decide(now=125)[0], "apply")            # still pending after a failure
        self.lock.applied(True)
        self.assertEqual(self.decide(now=150)[0], "ok")

    def test_unlocked_or_no_request_does_nothing(self):
        self.lock.pending.add("suspend/resume")
        self.assertEqual(self.decide(locked=False, reading=smu(tdp=5)), ("idle", [], False))
        self.assertEqual(self.lock.pending, set())
        self.assertEqual(self.decide(requested=None)[0], "idle")

    def test_over_cap_blocks_reapply(self):
        self.assertEqual(self.decide(requested=36, reading=smu(tdp=15))[0], "over_cap")
        self.assertEqual(self.decide(now=200, requested=31, vrm=True, reading=smu(tdp=15, edc_limit=60.0))[0],
                         "over_cap")

    def test_tick_uses_the_remembered_request(self):
        applied = []
        fanlib.update_settings({"tdp_requested": 20})
        fanlib._rt_set(tdp_locked=True, vrm_desired=False)
        old = fanlib.SMU
        fanlib.SMU = fanlib.SmuCache(reader=lambda: {"tdp": 15, "power": 5.0, "power_slow": 5.0, "edc": 1.0,
                                                     "edc_limit": 45.0, "tdc": 1.0, "tdc_limit": 35.0,
                                                     "thm_limit": 95.0})
        fanlib.TDP_LOCK.last_apply_mono = -1e9
        try:
            with mock.patch.object(fanlib, "apply_tdp", side_effect=lambda w, v, r: applied.append((w, v, r)) or (True, None)), \
                    mock.patch.object(fanlib, "control_temp_now", return_value=60.0):
                self.assertEqual(fanlib.tdp_lock_tick(), "apply")
                self.assertEqual(applied[0][:2], (20, False))
                self.assertIn("drift 15->20 W", applied[0][2])
                self.assertEqual(fanlib.tdp_lock_tick(), "floor")
                ev = threading.Event()
                ev.set()
                self.assertEqual(fanlib.tdp_lock_tick(ev), "stopped")   # quitting: never re-apply
        finally:
            fanlib.SMU = old
            fanlib._rt_set(tdp_locked=False)
            fanlib.update_settings({"tdp_requested": None})


class StartupTdpRuleTests(unittest.TestCase):

    def setUp(self):
        fanlib._rt_set(tdp_locked=False, startup_tdp_skipped=None)
        self.applied = []
        p = mock.patch.object(fanlib, "apply_tdp", side_effect=lambda w, v, r: self.applied.append((w, v)) or (True, None))
        p.start()
        self.addCleanup(p.stop)

    def settings(self, **kw):
        s = fanlib._sanitize_settings({"tdp_requested": 20, "tdp_locked": True, "apply_tdp_at_startup": True})
        s.update(kw)
        return s

    def test_not_opted_in(self):
        self.assertEqual(fanlib._apply_startup_tdp(self.settings(apply_tdp_at_startup=False), True), "not_requested")
        self.assertEqual((self.applied, fanlib._rt("tdp_locked"), fanlib._rt("startup_tdp_skipped")), ([], False, None))

    def test_unclean_exit(self):
        self.assertEqual(fanlib._apply_startup_tdp(self.settings(), False), "unclean_exit")
        self.assertEqual((self.applied, fanlib._rt("tdp_locked"), fanlib._rt("startup_tdp_skipped")),
                         ([], False, "unclean_exit"))

    def test_above_25w(self):
        self.assertEqual(fanlib._apply_startup_tdp(self.settings(tdp_requested=26), True), "above_25w")
        self.assertEqual((self.applied, fanlib._rt("startup_tdp_skipped")), ([], "above_25w"))

    def test_applied_with_stock_vrm_and_lock_honoured(self):
        fanlib._rt_set(vrm_desired=True)
        try:
            self.assertEqual(fanlib._apply_startup_tdp(self.settings(tdp_requested=25), True), "applied")
        finally:
            fanlib._rt_set(vrm_desired=False)
        self.assertEqual(self.applied, [(25, False)])
        self.assertTrue(fanlib._rt("tdp_locked"))
        fanlib._rt_set(tdp_locked=False)


# ─────────────────────────────────────────────────────────────────────────────
# Release path and server.py
# ─────────────────────────────────────────────────────────────────────────────

class ReleaseTests(BaseCase):

    def tearDown(self):
        fanlib._rt_set(released=False, workers_started=False)
        fanlib._stop_event.clear()
        super().tearDown()

    def test_release_resumes_our_override_and_marks_clean_exit(self):
        fanlib.update_settings({"last_clean_exit": False})
        fake = FakeDaemon()
        try:
            with mock.patch.object(fanlib, "read_control_temp_raw", return_value=45.0):
                self.assertTrue(post("/api/fan/hold", {"level": "3", "seconds": 600})[1]["success"])
            set_at = fake.requests and fanlib._rt("owned_set_at")
            daemon_up(reason="override", override=override_obj("3", set_at=set_at))
            fanlib._rt_set(vrm_desired=True)
            fanlib.release_all()
            self.assertEqual(fake.requests[-1], {"cmd": "resume"})
            self.assertIs(fanlib._rt("vrm_desired"), False)             # restore_stock ran
            self.assertIs(fanlib.get_settings()["last_clean_exit"], True)
            n = len(fake.requests)
            fanlib.release_all()                                         # idempotent
            self.assertEqual(len(fake.requests), n)
        finally:
            fake.close()

    def test_release_leaves_someone_elses_override_alone(self):
        fake = FakeDaemon()
        try:
            fanlib._rt_set(hold_owned=True, owned_set_at=111.0, owned_wall=time.time() - 60)
            daemon_up(reason="override", override=override_obj("5", set_at=222.0))
            fanlib.release_all()
            self.assertEqual(fake.requests, [])
        finally:
            fake.close()

    def test_release_ends_the_fallback_with_auto(self):
        daemon_down()
        writes = []
        with mock.patch.object(fanlib, "read_control_temp_raw", return_value=45.0):
            self.assertTrue(post("/api/fan/hold", {"level": "2", "seconds": 600})[1]["success"])
            with mock.patch.object(fanlib, "_fan_set_direct",
                                   side_effect=lambda l, w: writes.append((l, w)) or (True, None)):
                fanlib.release_all()
        self.assertFalse(fanlib.FALLBACK.active)
        self.assertEqual(writes, [("auto", 0)])

    def test_begin_session_marks_unclean(self):
        fanlib.update_settings({"last_clean_exit": True})
        s, clean = fanlib._begin_session()
        self.assertTrue(clean)
        self.assertIs(fanlib.get_settings()["last_clean_exit"], False)
        self.assertFalse(fanlib._begin_session()[1])


class ServerScriptTests(unittest.TestCase):

    def env(self):
        e = dict(os.environ, **ENV)
        e["FANCTL_SETTINGS_FILE"] = os.path.join(TMP, "srv", "settings.json")
        return e

    def test_port_in_use(self):
        r = subprocess.run([sys.executable, os.path.join(REPO, "server.py"), "--port", str(PORT), "--no-browser"],
                           env=self.env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Port in use — another copy is already running", r.stderr)
        self.assertFalse(os.path.exists(self.env()["FANCTL_SETTINGS_FILE"]))   # nothing started
        # An unusable port is reported as such, not as "another copy".
        r = subprocess.run([sys.executable, os.path.join(REPO, "server.py"), "--port", "70000", "--no-browser"],
                           env=self.env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Cannot listen on 127.0.0.1:70000", r.stderr)
        self.assertNotIn("another copy", r.stderr)
        self.assertFalse(os.path.exists(self.env()["FANCTL_SETTINGS_FILE"]))

    def test_sigterm_runs_the_release_path(self):
        probe = socket.socket()
        port = None
        for p in range(PORT + 1, 7200):
            try:
                probe.bind(("127.0.0.1", p))
            except OSError:
                continue
            port = p
            break
        probe.close()
        self.assertIsNotNone(port)
        proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py"), "--port", str(port), "--no-browser"],
                                env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            path = self.env()["FANCTL_SETTINGS_FILE"]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    with open(path) as f:
                        if json.load(f)["last_clean_exit"] is False:
                            break
                except (OSError, ValueError):
                    pass
                time.sleep(0.1)
            else:
                self.fail("server.py never marked the session unclean")
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/status", headers={"Host": f"localhost:{port}"})
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, err)
            with open(path) as f:
                self.assertIs(json.load(f)["last_clean_exit"], True)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


# ─────────────────────────────────────────────────────────────────────────────
# §14 "All POST replies: {success, error, ...extra}"
# ─────────────────────────────────────────────────────────────────────────────

class PostReplyShapeTests(BaseCase):

    ENDPOINTS = [
        ("/api/fan/hold", {"level": "3", "seconds": 60}), ("/api/fan/hold", {"level": "9"}),
        ("/api/fan/resume", {}), ("/api/fan/set", {"level": "4"}), ("/api/fan/set", {}),
        ("/api/daemon/start", {}), ("/api/daemon/stop", {}), ("/api/daemon/restart", {}),
        ("/api/tdp/set", {"tdp": 15}), ("/api/tdp/set", {"tdp": 99}),
        ("/api/tdp/lock", {"locked": False}), ("/api/tdp/lock", {"locked": "no"}),
        ("/api/tdp/vrm_unlock", {"unlocked": False}), ("/api/tdp/vrm_unlock", {}),
        ("/api/tdp/restore_stock", {}),
        ("/api/config", {"hysteresis": 5, "bogus_key": 1}),
        ("/api/settings", {"ui": {"view": "fan"}}), ("/api/settings", {"ui": {"view": 5}}),
        ("/api/alert/test", {}), ("/api/alert/test", {}),               # second one is rate-limited
    ]

    def test_every_post_reply_has_success_and_error(self):
        daemon_down()                      # dry-run fallback paths; nothing is actuated
        with mock.patch.object(fanlib, "read_control_temp_raw", return_value=45.0):
            for path, body in self.ENDPOINTS:
                status, r = post(path, body)
                self.assertEqual(status, 200, path)
                self.assertIsInstance(r["success"], bool, (path, body))
                if r["success"]:
                    self.assertIsNone(r["error"], (path, body))
                else:
                    self.assertIsInstance(r["error"], str, (path, body))
                    self.assertTrue(r["error"].strip(), (path, body))

    def test_config_post_reply(self):
        status, r = post("/api/config", {"hysteresis": 5, "bogus_key": 1, "schema": 7})
        self.assertEqual((status, r["success"], r["error"]), (200, True, None))
        self.assertEqual(r["config"]["hysteresis"], 5)
        self.assertEqual(r["config"]["schema"], 2)                     # never taken from the client
        self.assertEqual(r["rejected"], ["bogus_key"])
        self.assertTrue(r["dry_run"])
        self.assertEqual(get_json("/api/config")["hysteresis"], 6)     # the dry run wrote nothing


# ─────────────────────────────────────────────────────────────────────────────
# app.py: real-GI import and tray/notification logic, headless (§16)
#
# Runs in a subprocess with DISPLAY, WAYLAND_DISPLAY and the session bus
# removed from the environment: the Gtk.Application is constructed but never
# registered or run, no window or tray icon is created and no notification
# can reach the desktop (notify() is replaced by a recorder as well).
# ─────────────────────────────────────────────────────────────────────────────

APP_PROBE = r"""
import json, sys, time
try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
except (ImportError, ValueError) as e:
    print("SKIP " + repr(e))
    sys.exit(0)
sys.path.insert(0, sys.argv[1])
import app, fanlib                      # a Gdk/Gtk version clash fails right here
from gi.repository import GLib
app.Notify = None
out = {"hold_items": [lvl for lvl, _ in app.HOLD_ITEMS]}
a = app.FanControlApp(port=7199, start_hidden=True)      # constructed only, never registered
sent = []
a.notify = lambda title, body, urgency, icon: sent.append([title, body, urgency]) or False

def st(crit=False, override=None, active=True, **kw):
    s = {"daemon_active": active, "temp_c": 91.2, "critical_temp": 90, "fan_rpm": 6400,
         "mode": "critical" if crit else "curve", "manual_critical": False,
         "state": {"critical": {"active": crit}, "override": override} if active else None}
    s.update(kw)
    return s

def take():
    r = sent[:]
    del sent[:]
    return r

a.apply_status(st(), None); a.apply_status(st(crit=True), None); a.apply_status(st(crit=True), None)
out["critical_edge"] = take()
ov = {"level": "3", "remaining_s": 5}
a.apply_status(st(override=ov), None); a.apply_status(st(), None)
out["hold_expired"] = take()
a.apply_status(st(override=ov), None); a.apply_status(st(crit=True), None)
out["hold_dropped_at_critical"] = take()
a.apply_status(st(override=ov), None)
fanlib._rt_set(last_resume_mono=time.monotonic())
a.apply_status(st(), None)
out["hold_resumed_by_us"] = take()
fanlib._rt_set(last_resume_mono=-1e9)
a.apply_status(st(active=False, manual_critical=False), None)
a.apply_status(st(active=False, manual_critical=True), None)
out["manual_critical_edge"] = take()
end = {"reason": "expired", "level": "4", "ts": time.time()}
a.apply_status(st(active=False), end); a.apply_status(st(active=False), end)
out["fallback_end"] = take()
out["lines"] = [
    app.status_line({"temp_c": 78.4, "fan_rpm": 4000, "mode": "curve"}),
    app.status_line({"temp_c": 78.4, "fan_rpm": 4000, "mode": "hold",
                     "state": {"override": {"level": "3", "remaining_s": 754}}}),
    app.status_line({"temp_c": None, "fan_rpm": None, "mode": "firmware"}),
    app.status_line({"temp_c": 60, "fan_rpm": 3000, "mode": "manual_unprotected", "level": "5"}),
    app.status_line({"temp_c": 60, "fan_rpm": 3000, "mode": "manual_unprotected", "manual_fallback": True,
                     "manual_fallback_level": "2", "manual_fallback_remaining_s": 3725}),
]

# do_quit: release path on a worker thread, then quit() from the main loop.
fanlib.update_settings({"last_clean_exit": False})
loop = GLib.MainLoop()
finished = []
a.quit = lambda: (finished.append(True), loop.quit())
a.do_quit()
a.do_quit()                              # idempotent
GLib.timeout_add_seconds(20, loop.quit)
loop.run()
out["quit"] = {"finished": len(finished), "clean_exit": fanlib.get_settings()["last_clean_exit"],
               "released": fanlib._rt("released")}
print("RESULT " + json.dumps(out))
"""


class AppModuleTests(unittest.TestCase):

    def test_real_gi_import_and_tray_logic(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS")}
        env.update(ENV)
        env["FANCTL_SETTINGS_FILE"] = os.path.join(TMP, "app", "settings.json")
        r = subprocess.run([sys.executable, "-c", APP_PROBE, REPO], env=env, cwd=TMP,
                           capture_output=True, text=True, timeout=60)
        if r.stdout.startswith("SKIP"):
            self.skipTest(r.stdout.strip())
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        line = [l for l in r.stdout.splitlines() if l.startswith("RESULT ")]
        self.assertTrue(line, r.stdout[-2000:] + r.stderr[-2000:])
        out = json.loads(line[-1][len("RESULT "):])

        self.assertEqual(out["hold_items"], ["0", "1", "2", "3", "4", "5", "6", "7", "auto", "disengaged"])
        self.assertEqual([n[2] for n in out["critical_edge"]], ["critical"])        # edge, not level
        self.assertIn("91 °C", out["critical_edge"][0][1])
        self.assertEqual(len(out["hold_expired"]), 1)
        self.assertEqual(out["hold_expired"][0][2], "normal")
        self.assertIn("back on the automatic curve", out["hold_expired"][0][1])
        kinds = sorted(n[2] for n in out["hold_dropped_at_critical"])
        self.assertEqual(kinds, ["critical", "normal"])
        self.assertTrue(any("critical protection" in n[1] for n in out["hold_dropped_at_critical"]))
        self.assertEqual(out["hold_resumed_by_us"], [])
        self.assertEqual([n[2] for n in out["manual_critical_edge"]], ["critical"])
        self.assertEqual(len(out["fallback_end"]), 1)
        self.assertIn("it ran out", out["fallback_end"][0][1])
        self.assertEqual(out["lines"], [
            "78 °C · 4000 RPM · Curve",
            "Hold 3 · 12:34 left",
            "-- °C · Firmware auto (no daemon)",
            "60 °C · 3000 RPM · Level 5 — no daemon, no protection",
            "Hold 2 (no daemon) · 1:02:05 left",
        ])
        self.assertEqual(out["quit"], {"finished": 1, "clean_exit": True, "released": True})


if __name__ == "__main__":
    unittest.main(verbosity=2)
