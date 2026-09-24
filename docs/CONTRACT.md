# ThinkPad Fan Control v2 — implementation contract

This document is the single source of truth for the v2 rewrite. Every component
(daemon, backend, dashboard, installer) is implemented against it independently,
so every schema, path, protocol and behaviour below is binding. When something
here is ambiguous, prefer the safer behaviour for the hardware and say so in a
code comment.

Target: ThinkPad T495, Ryzen 5 PRO 3500U (Picasso), Linux Mint 22.3 (Ubuntu
24.04 base, systemd 255, Python 3.12, GTK3 + WebKit2GTK 4.1 2.52, Cinnamon/X11).
One physical fan; `thinkpad_acpi` exposes it as fan1 and fan2 with identical
values. `k10temp` Tctl and `amdgpu` edge read the same die; the controller uses
`max(cpu, igpu)`. Measured RPM on this unit: level 7 ≈ 4000, disengaged ≈ 6400.

VERSION = "2.0.0" (daemon `--version`, fanlib `VERSION`, `state.json.version`).

## 0. Ownership (who edits which files)

| Component | Files |
| --- | --- |
| **daemon** | `daemon/thinkpad-fan-controld`, `daemon/thinkpad-fan-control.service`, `daemon/fan-set-level.sh`, `daemon/fan-config-save.sh`, `daemon/fan-control.sudoers`, `daemon/thinkpad-fan-control.logrotate` (new), `daemon/daemon.env.example` (new), `tests/sim/run.sh` (new), `tests/test_daemon.py` (new) |
| **backend** | `fanlib.py`, `app.py`, `server.py`, `ryzenadj-set-tdp.sh`, `tests/test_fanlib_api.py` (new) |
| **dashboard** | `index.html` |
| **installer/docs** | `install.sh`, `README.md`, `launch.sh`, `uninstall.sh` (new) |

Nobody edits a file outside their row. `tests/sim/traces/real-2026-09-23-mini-eq-1hz.csv`
already exists (900 s of real 1 Hz data recorded today under a one-core load:
columns `ts,cpu,igpu,rpm,level,ac,gpu_busy`).

## 1. Filesystem layout

| Path | Owner / mode | Purpose |
| --- | --- | --- |
| `/usr/local/bin/thinkpad-fan-controld` | root 0755 | daemon |
| `/usr/local/bin/fan-set-level.sh` | root 0755 | direct EC write, used only when the daemon is not running |
| `/usr/local/bin/fan-config-save.sh` | root 0755 | `exec thinkpad-fan-controld --save-config` |
| `/usr/local/bin/ryzenadj-set-tdp.sh` | root 0755 | TDP / VRM wrapper |
| `/etc/thinkpad-fan-control/config.json` | root 0644 | daemon config, written ONLY by `--save-config` |
| `/etc/thinkpad-fan-control/daemon.env` | root 0644 | see §2 |
| `/etc/systemd/system/thinkpad-fan-control.service` | root 0644 | unit |
| `/etc/sudoers.d/fan-control` | root 0440 | sudoers |
| `/etc/logrotate.d/thinkpad-fan-control` | root 0644 | log rotation |
| `/run/thinkpad-fan-control/` | root 0755 (`RuntimeDirectory=`) | volatile runtime state, vanishes when the daemon stops |
| `/run/thinkpad-fan-control/state.json` | root 0644 (explicit `fchmod`) | §5 |
| `/run/thinkpad-fan-control/history.json` | root 0644 | §6 |
| `/run/thinkpad-fan-control/control.sock` | root:GUI_GID 0660 | §7 |
| `/var/lib/thinkpad-fan-control/rpm.json` | root 0644 (`StateDirectory=`) | learned RPM per level |
| `/var/log/thinkpad-fan-control.log` | root 0644 | daemon log |
| `/usr/local/share/thinkpad-fan-control/alert.wav` | root 0644 | fallback alert sound (copied from repo `alert.wav`) |
| `~/.config/thinkpad-fan-control/settings.json` | user 0600 | per-user GUI settings, §9 |

## 2. `daemon.env`

Written by `install.sh` (existing values preserved), read by the unit via
`EnvironmentFile=` and by `ryzenadj-set-tdp.sh` directly (`set -a; . file`).

```
FANCTL_GUI_UID=1000
FANCTL_GUI_GID=1000
FANCTL_GUI_HOME=/home/jhnlstrlclcn
FANCTL_GUI_RUNTIME_DIR=/run/user/1000
FANCTL_TDP_MAX=35
FANCTL_TDP_MAX_VRM_UNLOCKED=30
```

Defaults inside the daemon when a variable is missing: uid/gid 1000, home
`/home/<pw_name of uid>`, runtime dir `/run/user/<uid>`.

## 3. Config schema (`config.json`, schema 2)

The daemon's `sanitize(raw, base)` is the only validator that matters. Missing
or invalid fields fall back to `base` (the config already on disk), then to
DEFAULTS. Unknown keys are dropped. Legacy `poll_interval` is ignored and removed.

| Key | Default | Range / rule |
| --- | --- | --- |
| `schema` | 2 | written by the daemon |
| `sample_interval` | 1 | int 1..5 seconds |
| `smoothing_up_s` | 8 | int 2..30 (EMA time constant for the rising path) |
| `smoothing_down_s` | 30 | int 5..120 (EMA time constant for the falling path) |
| `dwell_down_s` | 45 | int 0..300, minimum time since the last UP step before any DOWN step |
| `step_down_spacing_s` | 10 | int 0..60, minimum time between two consecutive DOWN steps |
| `hysteresis` | 6 | int 0..20, default release gap for steps without an explicit `down` |
| `critical_temp` | 90 | int 70..105 |
| `critical_exit_margin` | 8 | int 3..20, exit critical when `T_slow < critical_temp - margin` |
| `critical_exit_hold_s` | 60 | int 10..600 |
| `watchdog` | 60 | 0, or int 15..120; if > 0 it is raised to at least `3*sample_interval + 15` |
| `override_max_seconds` | 7200 | int 60..14400 |
| `override_ceiling_temp` | `critical_temp - 8` | int 60..`critical_temp - 1` |
| `curve` | see below | 1..8 steps |
| `battery_curve` | see below | 1..8 steps |
| `use_battery_curve` | true | bool |
| `alerts_enabled` | true | bool |
| `alert_sound` | `/home/jhnlstrlclcn/Music/SYSTEM SOUND/90c.mp3` | absolute path string ≤ 400 chars; unreadable at play time → fallback wav |
| `alert_cooldown` | 300 | int 30..3600 |

Curve step: `{"temp": int, "level": str, "down": int (optional)}`.

Hard rules (root-side, invalid curve → the whole curve is rejected and the
previous one kept; the key is reported in `rejected`):

* 1..8 steps; `steps[0].temp` is forced to 0; temps strictly increasing ints in 0..105.
* `level` ∈ `{"auto","0".."7","disengaged","full-speed"}`; `"auto"` is allowed **only** at index 0.
* Level rank must be non-decreasing with temperature. Rank: `"0".."7"` → 0..7, `disengaged`/`full-speed` → 8, `auto` → -1.
* For steps with `temp >= 60`, rank must be ≥ 1 (no fan-off when warm).
* `down`, when present: int with `temp - 20 <= down <= temp - 1`. When absent the
  effective release temperature is `max(0, temp - hysteresis)`, computed at
  decision time (not stored).

DEFAULTS:

```
curve:         [{temp:0,level:"auto"},{temp:50,level:"4"},{temp:60,level:"6"},{temp:70,level:"7"},{temp:80,level:"disengaged",down:72}]
battery_curve: [{temp:0,level:"auto"},{temp:60,level:"3"},{temp:70,level:"5"},{temp:80,level:"7"},{temp:87,level:"disengaged",down:78}]
```

v1 → v2 migration (applied by `load_config()` and by `--save-config` whenever
`schema` is missing): drop `poll_interval`; `watchdog == 0` → 60 (log once
"watchdog enabled (60 s) by upgrade"); a curve byte-equal to the v1 defaults
(same steps without `down`) → the v2 default for that curve; add missing keys.

`--save-config`: read at most 65536 bytes from stdin (more → exit 1), parse a
JSON object, `sanitize(raw, base=on-disk)`, write atomically (tmp + fsync +
`os.replace`, mode 0644), log "Config updated from GUI", and print exactly one
JSON line to stdout: `{"config": <sanitized>, "rejected": ["battery_curve", ...]}`
where `rejected` lists top-level keys that were present in `raw` but not
accepted. Exit 0 on success, 1 on bad input.

## 4. Controller algorithm (daemon)

Runs every `sample_interval` seconds inside a `selectors` loop (see §7). Let
`dt = sample_interval`, `a_up = 1 - exp(-dt/smoothing_up_s)`,
`a_dn = 1 - exp(-dt/smoothing_down_s)`.

Sensors: `k10temp temp1_input` and `amdgpu temp1_input` read as floats (°C, not
integer-truncated); a value outside `(0, 150)` is invalid. `raw = max(valid)`;
`None` when both are invalid. Sensors are found by hwmon **name**, never by number.

Per tick, in this order:

1. **Sensor loss.** If `raw is None`: `missing += 1`; after 3 consecutive misses
   set `sensor_lost = True` once (log once), command `"disengaged"` if critical
   is active else `"auto"`, reason `sensor_lost`, skip the rest of the tick. On
   recovery: log once, seed `T_fast = T_slow = raw`, `reseat()`.
2. **Filters.** `T_fast += a_up*(raw-T_fast)`; `T_slow += a_dn*(raw-T_slow)`.
   On startup both are seeded with the first valid `raw`.
3. **Power source.** Read `/sys/class/power_supply/AC/online` (missing → AC).
   Switch curves only after 3 consecutive identical readings; on a switch log
   "Power -> battery|AC" and `reseat()`. `battery_curve` is used only when
   `use_battery_curve` is true.
4. **Config reload.** If the config file mtime changed: reload with
   `sanitize(parsed, base=current_cfg)`; on read/parse error keep the current
   config and log once. If the sanitized result differs from the running config:
   log "Config reloaded", `reseat()`, re-arm the watchdog only if its value
   changed. Never reset the EMAs or drop an override on reload.
5. **Critical.** Entry (when not active): `raw >= critical_temp` on two
   consecutive samples, or `raw >= critical_temp + 3` once. On entry: log
   "CRITICAL entered (raw …)", `critical_since = now`, alert (§8) if
   `alerts_enabled` and `now - last_alert >= alert_cooldown`; while active,
   alert again every `alert_cooldown`, logging "ALERT: critical since
   HH:MM:SS (raw …, slow …)" (never "raw X at or above critical", because the
   current raw may already be below it). Exit: `T_slow < critical_temp -
   critical_exit_margin` **and** `now - critical_since >= critical_exit_hold_s`
   → log "CRITICAL cleared (slow …) -> top step", then set `idx = n - 1` and
   `last_up = last_change = now` (**not** `reseat()`): the fan leaves critical
   onto the top curve step and the normal DOWN rules (release temperature,
   dwell, spacing) bring it down. Re-seating by `T_fast` here dropped straight
   to level 7, re-heated and re-entered critical (simulated load15: 25–35
   changes/h, driven by exactly this).
6. **Curve evaluation** (always computed, even while an override or critical is
   commanding the fan, so `curve_level` is always "what the curve would do"):
   * UP: if `idx+1 < n` and `T_fast >= curve[idx+1].temp`: `idx += 1`,
     `last_up = now`, `last_change = now` (one step per tick; another step
     follows next tick if still above).
   * else DOWN: if `idx > 0` and `T_slow < down(idx)` and `now - last_up >=
     dwell_down_s` and `now - last_change >= step_down_spacing_s`: `idx -= 1`,
     `last_change = now`.
   * `reseat()`: `idx = max i with curve[i].temp <= T_fast`; `last_up =
     last_change = now`. Used at start, on config reload, AC switch, sensor
     recovery and override end (critical exit has its own rule in step 5).
     Never touches the EMAs.
7. **Target selection**, first match wins:
   * critical active → `"disengaged"`, reason `critical`.
   * override active (§7) and not suspended → `override.level`, reason `override`.
     Suspension rule: if `override.level != "auto"` and `T_fast >= override_ceiling_temp`
     and `rank(curve_level) > rank(override.level)` → `suspended = True`,
     `suspended_at = now`; resume the override only when `T_slow <
     override_ceiling_temp - hysteresis` **and** `now - suspended_at >= 30 s`.
     Suspend and resume each log one line. While suspended the target is
     `curve_level`, reason `override_suspended_hot`. (Using raw here flapped
     3 -> disengaged -> 3 within 2 s on a single Tctl spike in simulation.)
   * otherwise `curve[idx].level`, reason `curve`.
8. **Override expiry.** If `override.until <= time.time()`: log "Override
   <level> ended (expired)", clear it, `reseat()`.
9. **Fan write.** `Fan.set(level, reason)` writes `level <x>` to
   `/proc/acpi/ibm/fan` only when the level changed **or** the refresh interval
   elapsed. Refresh interval: `min(20, watchdog // 3)` when `watchdog > 0`, else
   30 s. Refresh writes are never logged. On a change, log exactly one line in
   the dense format (§10). Also parse `/proc/acpi/ibm/fan` each tick
   (`level:`, `speed:`, `status:`) for `state.level_proc` / `state.rpm`; if
   `level_proc` differs from the commanded level and is not `auto` (a watchdog
   revert reads `auto`), log once "external fan write detected (level X)" and
   re-assert the commanded level; set `state.external_write_detected` for 60 s.
10. **Watchdog.** Write `watchdog <n>` at start and whenever the effective value
    changes. `n = 0` when the config says 0, else `max(watchdog, 3*dt+15)`.
11. **RPM learning.** After ≥ 20 s at a commanded level, push the EC speed into
    a per-level `deque(maxlen=50)`; the median is `state.rpm_by_level[level]`
    and is persisted to `/var/lib/thinkpad-fan-control/rpm.json` when it changes
    by ≥ 50 RPM (best effort; a missing dir is not fatal).
12. **State publication.** Write `state.json` every tick and immediately after
    any change (§5). Append a history sample every tick; write `history.json`
    every 10 s and after any event (§6).
13. `top_cycles_10min`: number of entries into the last curve step during the
    last 600 s (for the "top-band cycling" UI hint).

Shutdown (SIGTERM/SIGINT): log "Daemon stopping — returning fan to firmware
control", write `watchdog 0` then `level auto`, exit 0. Startup: wait up to 60 s
(2 s steps) for `/proc/acpi/ibm/fan` and a readable sensor, then **self-test**:
read the current level, write it back; if that write fails, log FATAL and exit 1
(the unit uses `Restart=on-failure` with a start limit). Then send
`READY=1` to `$NOTIFY_SOCKET` if set (Type=notify) and `WATCHDOG=1` at least
every 15 s from the loop.

Clocks: `time.monotonic()` for dwell/spacing/hold/cooldown math;
`time.time()` only for `override.until`, `level_since`, `ts` fields and the log.

## 5. `state.json`

Atomic write (`os.open(tmp, O_WRONLY|O_CREAT|O_TRUNC, 0o644)`, `os.fchmod(fd,
0o644)`, write, close, `os.replace`). Fields (all present every time; `null`
where not applicable):

```
schema: 1, version: "2.0.0", pid, ts (epoch float), mono (monotonic float),
sample_interval, sensor_ok (bool), temp_raw, temp_fast, temp_slow, cpu, igpu,
level (commanded), level_proc (from /proc), rpm (from /proc "speed"),
fan_control_available (bool: /proc/acpi/ibm/fan has a "commands:" line containing "level <level>"),
fan_status ("enabled"|"disabled"|null: the raw /proc "status:" word — "disabled" just means the EC fan register is 0, i.e. level 0 / fan stopped),
reason: "curve"|"critical"|"override"|"override_suspended_hot"|"sensor_lost",
on_ac (bool), curve: "ac"|"battery", step_index, steps (count), curve_level,
up_at (next step temp or null), down_below (release temp of the current step or null),
level_since (epoch), dwell_remaining_s (seconds until a DOWN is allowed, 0 if allowed),
critical: {active: bool, since: epoch|null, alerts: int},
override: null | {level, until (epoch), set_at (epoch), remaining_s, suspended: bool},
watchdog (effective seconds), hysteresis, critical_temp, config_mtime, config_schema,
rpm_by_level: {"7": 4000, "disengaged": 6400, ...},
top_cycles_10min (int), external_write_detected (bool)
```

Liveness rule used by everyone: the daemon is **active** iff `state.json`
exists, parses, and `time.time() - ts < 3*sample_interval + 2`.

## 6. `history.json`

```
{"schema": 1, "sample_interval": 1,
 "samples": [[ts, raw, fast, slow, level, rpm, ac], ...],   // ≤ 1800 (30 min at 1 Hz), oldest first
 "events":  [[ts, kind, detail], ...]}                       // ≤ 200
```

`kind` ∈ `level | critical_on | critical_off | override_start | override_end |
config | daemon_start | ac | sensor_lost | sensor_ok | alert`. `detail` is a
short string (e.g. `"7->disengaged"`). Written every 10 s and after any event.

## 7. Control socket

`AF_UNIX` / `SOCK_STREAM` at `/run/thinkpad-fan-control/control.sock`; after
`bind`: `os.chown(path, 0, GUI_GID)`, `os.chmod(path, 0o660)`, `listen(4)`. The
daemon handles connections inline in its `selectors` loop (no threads):
`settimeout(0.5)`, read up to 4096 bytes up to the first `\n`, check
`SO_PEERCRED` uid ∈ `{0, GUI_UID}` (else close silently), parse one JSON
object, reply with exactly one JSON line, close.

Requests and replies:

| Request | Behaviour | Reply |
| --- | --- | --- |
| `{"cmd":"hold","level":L,"seconds":S}` | `L` ∈ valid levels; `S` int, `0` → `override_max_seconds`, clamped to it. Rejected with `too_hot_for_fan_off` when `L == "0"` and `raw >= 55`; with `fan_control_unavailable` when `/proc/acpi/ibm/fan` has no `commands:` line offering `level` (thinkpad_acpi loaded without `fan_control=1`). **Never** reject because `status:` reads `disabled`: that only means the fan is currently at level 0. Sets `override = {level, until: time.time()+S, set_at, suspended: false}`, evaluates the tick logic immediately (the fan write happens before the reply), logs "Override <L> for <S>s (uid N)". | `{"ok":true,"state":<state>}` |
| `{"cmd":"resume"}` | Clears any override (ok even if none), `reseat()`, applies immediately. | `{"ok":true,"state":<state>}` |
| `{"cmd":"status"}` | — | `{"ok":true,"state":<state>}` |
| `{"cmd":"test_alert"}` | Plays the configured sound (§8), rate-limited to one per 10 s. | `{"ok":true}` or error `rate_limited` |
| anything else | — | `{"ok":false,"error":"bad_request","message":"…"}` |

Error reply shape: `{"ok":false,"error":"<snake_code>","message":"<human text>"}`.
The override is in-memory only: a daemon restart or reboot drops it.

## 8. Alerts (daemon)

`play_sound(path)`: `subprocess.Popen([player, path], user=GUI_UID,
group=GUI_GID, extra_groups=[], cwd="/", env={XDG_RUNTIME_DIR, PULSE_SERVER=
"unix:<runtime>/pulse/native", PATH="/usr/bin:/bin", HOME=GUI_HOME},
stdin/stdout/stderr=DEVNULL, start_new_session=True)`. No `sudo`. Player:
`paplay` first for any extension; if it exits non-zero within 2 s, retry with
`gst-play-1.0 --no-interactive --volume=1.0`. Children are tracked and killed
after 30 s; failures are logged once. If `alert_sound` is unreadable use the
fallback wav. The GUI raises its desktop notification **edge-triggered** on
`state.critical.active` becoming true, never on its own temperature reading.

## 9. Per-user `settings.json` (backend)

`~/.config/thinkpad-fan-control/settings.json`, dir 0700, file 0600, atomic write:

```
{"schema":1, "tdp_requested": null|int, "tdp_locked": false, "apply_tdp_at_startup": false,
 "last_clean_exit": true,
 "ui": {"hold_default_seconds": 900, "history_range_min": 15, "log_filter": "all", "view": "overview"}}
```

`vrm_unlocked` is **never** persisted; VRM unlock is session-only. At startup the
backend sets `last_clean_exit=false`; `do_quit`/clean exit sets it back to true.
TDP is applied at startup only if `apply_tdp_at_startup and last_clean_exit and
5 <= tdp_requested <= 25`, always with stock VRM (`0`); only then is
`tdp_locked` honoured from the start. When skipped, `status.startup_tdp_skipped`
carries the reason (`"unclean_exit"` | `"above_25w"` | `null`).

## 10. Log format

`[YYYY-mm-dd HH:MM:SS] <message>` appended with `open(LOG, "a",
encoding="utf-8")` per line (logrotate-compatible, no self-rotation). Level
change line: `Fan 7 -> disengaged (raw 81.2 fast 80.1 slow 77.9 | step 4/5 ac |
up>=80 down<72) [curve]` with the reason in brackets. Startup line includes the
version, sample interval, watchdog and a curve summary like
`0:auto 50:4 60:6 70:7 80/72:disengaged`. Identical WARN/ERROR text is
rate-limited to once per 60 s (with a `(xN)` count when it resumes). Never log
watchdog refreshes or per-sample data. Readers must strip `\x00` (the file has
NUL padding from past hard crashes) and decode with `errors="replace"`.

`daemon/thinkpad-fan-control.logrotate`:
```
/var/log/thinkpad-fan-control.log {
    size 2M
    rotate 2
    missingok
    notifempty
    compress
    delaycompress
    create 0644 root root
}
```

## 11. systemd unit (`daemon/thinkpad-fan-control.service`)

```
[Unit]
Description=ThinkPad fan control daemon
After=systemd-modules-load.service local-fs.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=45
ExecStart=/usr/local/bin/thinkpad-fan-controld
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
KillMode=mixed
TimeoutStopSec=10
EnvironmentFile=-/etc/thinkpad-fan-control/daemon.env
RuntimeDirectory=thinkpad-fan-control
RuntimeDirectoryMode=0755
StateDirectory=thinkpad-fan-control
UMask=0077
ProtectSystem=strict
ReadWritePaths=/var/log
ProtectHome=read-only
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
ProcSubset=all
RestrictAddressFamilies=AF_UNIX
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_CHOWN CAP_FOWNER CAP_DAC_OVERRIDE
NoNewPrivileges=yes
IPAddressDeny=any
DevicePolicy=closed
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**Never** set `ProtectKernelTunables=yes`, `ProcSubset=pid`,
`MemoryDenyWriteExecute=yes`, `User=`/`DynamicUser=` or `PrivateUsers=`: on this
host they make `/proc/acpi/ibm/fan` read-only or break the sound player. The
daemon must verify every fan write's return and exit 1 at startup if the
self-test write fails. `ProtectSystem=strict` leaves `/proc` writable (it is not
part of the protected mounts), and `/run/thinkpad-fan-control` and
`/var/lib/thinkpad-fan-control` are writable through `RuntimeDirectory=`/`StateDirectory=`.
`CAP_DAC_OVERRIDE` is kept so the root daemon can still write the log if a
rotation left it with another mode; drop it if the self-test shows it is not needed.

## 12. Root wrappers and sudoers

`daemon/fan-set-level.sh <level> [watchdog]`: `level` whitelisted exactly as
today; optional `watchdog` must match `^(0|[1-9][0-9]?|1[01][0-9]|120)$` and is
written **before** the level. Used by the backend only when the daemon is down.

`ryzenadj-set-tdp.sh <watts> <vrm>`: `watts` must match `^([5-9]|[1-3][0-9]|40)$`
(no leading zeros), `vrm` must match `^[01]$`. Source `daemon.env` if present;
refuse (exit 2, message on stderr) when `watts > FANCTL_TDP_MAX`, or `vrm == 1`
and `watts > FANCTL_TDP_MAX_VRM_UNLOCKED`. Stock VRM: `--vrmmax-current=45000
--vrm-current=35000`; unlocked: `60000 / 42000`; both `--tctl-temp=95`. Print
`applied <watts>W vrm=<0|1>` on success.

`daemon/fan-control.sudoers` (install.sh substitutes the user name):

```
jhnlstrlclcn ALL=(root) NOPASSWD: /usr/local/bin/fan-set-level.sh
jhnlstrlclcn ALL=(root) NOPASSWD: /usr/local/bin/fan-config-save.sh
jhnlstrlclcn ALL=(root) NOPASSWD: /usr/local/bin/ryzenadj-set-tdp.sh
jhnlstrlclcn ALL=(root) NOPASSWD: /usr/local/bin/ryzenadj --info
jhnlstrlclcn ALL=(root) NOPASSWD: /usr/bin/systemctl start thinkpad-fan-control.service, /usr/bin/systemctl stop thinkpad-fan-control.service, /usr/bin/systemctl restart thinkpad-fan-control.service
Defaults!/usr/local/bin/ryzenadj !syslog
```

## 13. Daemon CLI

```
thinkpad-fan-controld                 run (systemd)
thinkpad-fan-controld --version
thinkpad-fan-controld --save-config   stdin JSON → config.json, prints {"config","rejected"}
thinkpad-fan-controld --self-test     wait for /proc + sensor, read/write-back level, write state.json to $RUNTIME_DIRECTORY or /tmp, exit 0/1; never plays sound
thinkpad-fan-controld --simulate <trace.csv|history.json> [--config FILE] [--set key=value ...] [--json] [--quiet]
thinkpad-fan-controld --gen-trace <idle|load12|load15|load18|stop|ramp|acflip|sensorloss|override> --seconds N [--seed S]
```

`--simulate` runs the exact decision code with a fake clock and a fake `Fan`
(no hardware, no files under /run, no sound) and prints the transitions plus a
summary: `changes_per_hour, transitions, median_dwell_s, min_dwell_s,
min_gap_opposite_s (7->dis->7 style reversals), seconds_at_or_above_critical,
time_share_per_level, alerts`. With `--json` the summary is one JSON object.
CSV columns accepted: `ts|t_s, cpu|tctl_c, igpu|gpu_c (optional), ac (optional),
sensor_ok (optional)`; extra columns ignored. `--set` overrides config keys for
what-if runs (`--set dwell_down_s=30`). `--gen-trace` uses a first-order thermal
model `T' = (T_amb + P*R(level) - T)/(R(level)*C)` with `C=50 J/K`, `T_amb=36`,
`R = {auto 4.8, "3":4.4, "4":4.2, "5":3.9, "6":3.5, "7":3.2, disengaged:2.25} K/W`,
THM clamp 95, plus Tctl bursts (+5..14 °C for 1–3 s about every 25 s under
load, every 60 s idle) and, when idle, 3–6 s +8 °C load bursts every 15–40 s.

`tests/sim/run.sh` (daemon owner) generates each scenario, runs `--simulate`
with the default config over seeds 1..10 (one hour each, deterministic) and
asserts, on the mean across seeds unless stated: `idle ≤ 10 changes/h`;
`load15 ≤ 30/h` (and no single seed above 40) with no reversal closer than
10 s on any seed; `load12 ≤ 10/h`; `stop`: first DOWN within
60 s of load end and no UP during the cascade; `ramp`: critical entered within
2 samples of the second ≥ 90 reading, exactly one alert per cooldown, exit only
after the hold; `acflip`: one transition per real flip, none for a 1-sample
bounce; `sensorloss`: auto after 3 missing samples, single log line, re-seat on
recovery; `override`: level held through 85 °C, suspended above the ceiling,
dropped at critical, re-seated on expiry. It also replays
`tests/sim/traces/real-2026-09-23-mini-eq-1hz.csv` through the v2 controller
and through the v1 rules (`--set legacy=1` may implement the old
raw-threshold/5 °C logic for comparison) and asserts ≥ 3× fewer transitions.
The script exits non-zero on any failed assertion and prints a table.

## 14. Backend HTTP API (fanlib)

Bound to `127.0.0.1:7070` (`--port` overrides). Every response carries
`Cache-Control: no-store` and `X-Content-Type-Options: nosniff`; `/` also
carries `Content-Security-Policy: default-src 'self' 'unsafe-inline';
connect-src 'self'; img-src 'self' data:`.

Request gates (in order): `Host` must be `127.0.0.1:<port>` or
`localhost:<port>` (else 421, empty body). For POST: `Content-Type` must start
with `application/json` (else 415); if `Origin` is present it must be
`http://127.0.0.1:<port>` or `http://localhost:<port>` (else 403); if
`Sec-Fetch-Site` is present it must be `same-origin` or `none` (else 403);
`Content-Length` parsed in try/except (bad → 400), `> 65536` → 413 before
reading. `OPTIONS` → 403. Never emit `Access-Control-Allow-*`.
`Handler.timeout = 10`.

All POST replies: `{"success": bool, "error": str|null, ...extra}`; errors are
human-readable sentences.

GET:

| Path | Returns |
| --- | --- |
| `/` , `/index.html` | the dashboard |
| `/api/status` | §15 |
| `/api/config` | the sanitized config as on disk (same shape as §3) |
| `/api/history?minutes=N` | `{"samples":[{t,cpu,igpu,ctrl,fast,slow,rpm,level,ac,tdp,power}],"events":[{t,kind,detail}]}`; N default 30, max 30. Samples come from the daemon's `history.json` (`ctrl`=raw); `tdp`/`power` are merged from the backend's own 2 s sampler (`deque(maxlen=900)` of `{t,tdp,power}` copied from the SMU cache, never forcing a refresh) by nearest timestamp within 5 s, else null. Empty arrays when the daemon is down. |
| `/api/log?lines=N` | text/plain, last N lines (default 200, max 2000), NUL-stripped, decoded with `errors="replace"` |
| `/api/settings` | §9 object |

POST:

| Path | Body | Behaviour |
| --- | --- | --- |
| `/api/fan/hold` | `{"level": L, "seconds": S}` | Daemon up: socket `hold` → `{"success":true,"fallback":false,"state":…}`. Daemon down: `sudo -n fan-set-level.sh L 120`, start the keep-alive thread (re-write L every 30 s; if control temp ≥ `critical_temp` write `disengaged` instead and set `manual_critical=true`; stops on `resume`, on daemon reappearance, or on quit) → `{"success":true,"fallback":true}`. Errors pass the daemon's message through. |
| `/api/fan/resume` | `{}` | Daemon up: socket `resume`. Daemon down: `fan-set-level.sh auto 0`, stop keep-alive. |
| `/api/fan/set` | `{"level": L}` | Legacy alias for `hold` with `seconds = settings.ui.hold_default_seconds`. |
| `/api/daemon/start\|stop\|restart` | `{}` | `sudo -n systemctl …` (Advanced only). |
| `/api/tdp/set` | `{"tdp": W}` | int 5..`tdp_max` (and ≤ `tdp_max_vrm_unlocked` when `vrm_desired`) else error; runs the wrapper with `vrm = 1 if vrm_desired else 0`; on success `tdp_requested = W` (persisted), SMU cache forced refresh. |
| `/api/tdp/lock` | `{"locked": bool}` | Persist `tdp_locked`. If locking with `tdp_requested is None`, set it from the current SMU value **only if** `smu.ok`. |
| `/api/tdp/vrm_unlock` | `{"unlocked": bool}` | Session-only `vrm_desired`. Unlocking with `tdp_requested > tdp_max_vrm_unlocked` → error "Lower the TDP to N W or below before unlocking". Re-applies `tdp_requested` (or the current SMU value) with the new flag. |
| `/api/tdp/restore_stock` | `{}` | Applies `tdp_requested or 15` with `vrm 0`; sets `vrm_desired=false`. |
| `/api/config` | partial config | `sudo -n fan-config-save.sh` with the JSON on stdin → `{"success", "config", "rejected"}` from the daemon's stdout. |
| `/api/settings` | partial §9 | Merge + save. Accepted keys: `ui.*`, `apply_tdp_at_startup`, `autostart_enabled` (rewrites `X-GNOME-Autostart-enabled=` in `~/.config/autostart/fan-control.desktop` if that file exists). |
| `/api/alert/test` | `{}` | Daemon up: socket `test_alert`; down: play locally with `paplay`/`gst-play-1.0` as the user. |

TDP lock loop (backend thread, 5 s tick): re-apply only if `tdp_locked and
tdp_requested is not None and smu.ok` and (`abs(smu.tdp - tdp_requested) >= 1`
or `vrm_unlocked != vrm_desired` or a suspend/resume was detected
(`wall_delta - mono_delta > 5 s`) or the power source changed), never more than
once per 20 s, and **paused** (flag `tdp_lock_paused_thermal`) while the control
temperature ≥ `critical_temp - 3`. Each re-apply is printed with its reason.

SMU polling: `ryzenadj --info` TTL 10 s while the window is visible, 30 s while
hidden/tray (`fanlib.set_visibility(bool)`, called by app.py). A non-zero exit
or a table without `STAPM LIMIT` raises inside the cached call so the last good
value is kept and `smu.ok=false` / `smu.stale=true` are reported; never
synthesize `22 W`.

Actuator dry-run: when the environment variable `FANCTL_DRY_RUN=1` is set,
every `sudo`/socket actuation is skipped and reported as success (used by
`tests/test_fanlib_api.py`); reading sensors is still real.

## 15. `/api/status` object

```
version, ts,
temps: {cpu, igpu, nvme, wifi},              // ints or null; wifi from the hwmon whose name starts with "iwlwifi"
temp_c,                                       // control temp: state.temp_raw when daemon active else max(cpu, igpu)
fan_rpm, fan1_rpm (alias, one release), level (from /proc), speed,
fan_control_available,                        // /proc/acpi/ibm/fan lists "commands:\tlevel …" (fan_control=1 active)
fan_enabled,                                  // compatibility alias of fan_control_available (NOT the /proc "status:" word)
fan_status,                                   // raw "status:" word; "disabled" == fan stopped at level 0, not an error
cpu_mhz_avg, gpu_mhz, gpu_busy, loadavg1,     // /proc/cpuinfo avg, amdgpu freq1_input/1e6, /sys/class/drm/card*/device/gpu_busy_percent, /proc/loadavg
governor, boost,                              // cpufreq scaling_governor, boost flag (int or null)
on_ac, battery_pct, battery_status, battery_watts,
daemon_active, daemon_enabled (systemctl is-enabled, cached 60 s), state (§5 object or null), state_age_s,
mode: "curve"|"hold"|"hold_suspended"|"critical"|"firmware"|"manual_unprotected"|"sensor_lost",
manual_fallback (keep-alive thread active), manual_critical,
smu: {ok, stale, tdp, power, power_slow, edc, edc_limit, tdc, tdc_limit, thm_limit, updated_at},
tdp, power, edc, edc_limit, tdc, tdc_limit, thm_limit,   // flattened copies for compatibility
tdp_requested, tdp_locked, tdp_lock_paused_thermal, apply_tdp_at_startup, startup_tdp_skipped,
vrm_unlocked,                                 // HARDWARE truth: smu.edc_limit >= 55
vrm_desired,                                  // session intent
tdp_max, tdp_max_vrm_unlocked,                // from daemon.env (defaults 35 / 30)
freeze_config_warning,                        // tdp >= 30 and vrm_unlocked
critical_temp, config_mtime, hold_default_seconds
```

`mode` derivation (single function, also used by the tray): not
`daemon_active` → `"firmware"` if `level == "auto"` else `"manual_unprotected"`;
else `state.reason` mapped: `critical` → `critical`, `override` → `hold`,
`override_suspended_hot` → `hold_suspended`, `sensor_lost` → `sensor_lost`,
else `curve`.

## 16. GTK app (`app.py`)

* Construct `Gtk.Application(application_id="com.thinkpad.fancontrol")` **first**; bind
  the HTTP port in the `startup` handler (only the primary instance gets it);
  `activate` presents the window, so a second launch raises the existing window.
  If the bind fails in the primary (e.g. `server.py` owns the port) show a
  `Gtk.MessageDialog` and exit 1.
* WebView created lazily on first present; while the window is hidden it shows
  `about:blank`; on present it loads `http://127.0.0.1:<port>/?embedded=1`.
  `HardwareAccelerationPolicy.ON_DEMAND`. Developer extras on.
* Tray (AppIndicator): label `" 78°"`, tooltip/title; menu: status line (`78 °C ·
  4000 RPM · Curve` / `Hold 3 · 12:34 left`), "Open dashboard", "Hold fan at…"
  submenu with 0–7, "Firmware auto", "Maximum" each using
  `settings.ui.hold_default_seconds` (label shows the duration), "Resume auto
  curve", "Restore stock power limits" (visible only while `vrm_unlocked`),
  separator, "Quit". No TDP presets and no daemon start/stop in the tray.
* One poller: `fanlib.get_status()` every 3 s on a thread, `GLib.idle_add` to
  apply. Desktop notification (libnotify, CRITICAL urgency) when
  `state.critical.active` flips to true; a second, normal-urgency notification
  when a hold ends by itself.
* `do_quit`: if this app created the current override, `resume`; if a
  daemon-less fallback is active, `fan-set-level.sh auto 0`; if this session
  unlocked VRM, `restore_stock`; `settings.last_clean_exit = true`; `Notify.uninit`.
  Also on SIGTERM and SIGINT.
* If no AppIndicator library is available, ignore `--tray` and show the window.
* Startup TDP rule from §9.

`server.py`: bind first inside try/except (print "Port in use — another copy is
already running", exit 1), then start a `daemon=True` browser timer; print the
URL; clean shutdown calls the same release path as `do_quit`.

## 17. Dashboard (`index.html`)

Single self-contained file, no network, dark theme, fonts `Ubuntu, Cantarell,
"Noto Sans", "DejaVu Sans", sans-serif` and `"Ubuntu Mono", "DejaVu Sans Mono",
monospace`, `font-variant-numeric: tabular-nums` on numbers. No emoji: icons are
one inline `<svg><symbol>` sprite used via `<use>`. No blinking, no entrance
animations, transitions ≤ 200 ms, `prefers-reduced-motion` honoured, 2 px focus
outlines. Temperature ramp: `<50 #00e5cc`, `50–64 #ffb400`, `65–79 #ff8c42`,
`≥ critical #ff3b5c` (the last boundary follows `critical_temp`, the middle ones
follow the top two curve steps when available). Level palette: `auto #6b7a90,
0 #1f8a99, 1 #22a3a8, 2 #27b8b0, 3 #3fc9a3, 4 #a8c94a, 5 #e0b628, 6 #f09a2a,
7 #f5702e, disengaged #ff3b5c`. `?embedded=1` hides the in-page title row.

Structure: a sticky **status strip** (control temp with CPU/GPU inputs and a
trend arrow, fan level + RPM + 10-segment level meter, one **mode pill**, power
source, TDP with a warning icon when `freeze_config_warning`, daemon dot
green/grey/amber(stale)/red(unreachable), contextual primary button "Resume
curve" / "Start daemon"), then static **banners** (critical; fan control
disabled; manual without daemon = "no thermal protection"; freeze-prone power
config worded as "preceded the 2026-09-15 GPU hang, not proven causal"; backend
unreachable), then a **view switcher** with four views (Ctrl+1..4, last view
persisted through `/api/settings` `ui.view` and mirrored in `localStorage`):

1. **Overview**: sensor tiles (CPU with MHz/load sub-line, iGPU with MHz/busy,
   NVMe, Wi-Fi; `n/a` when null), the **history chart**, the **Fan card**, a
   power summary.
2. **Fan curve**: the **curve editor** (SVG step chart + mirrored table),
   **Response** card (hysteresis, dwell, smoothing preset Off/Light/Normal/Heavy
   → `(smoothing_up_s, smoothing_down_s)` = (2,5)/(4,15)/(8,30)/(12,60) with an
   advanced disclosure for the raw numbers, critical temp, critical exit
   margin), **Daemon & alerts** card (daemon status + Advanced disclosure with
   confirm for stop/restart, sample interval, watchdog Off/60/90/120, alerts
   switch, alert sound path + Test, cooldown, log path/size).
3. **Power**: TDP card (readouts STAPM/fast/slow/EDC/TDC/THM with meters; zoned
   slider 5..`tdp_max` (stock ≤15 teal, 16–25 amber, >25 red); explicit Apply;
   presets Quiet 12 / Stock 15 / Boost 25 / Last used; "Requested vs SMU
   reports" line; lock switch with sub-copy; apply-at-startup switch with the
   ≤25 W / clean-exit rule explained; VRM section with hardware state text and
   a two-step Unlock button (arm for 5 s, second click confirms) + single-click
   Restore stock), Battery/AC card (source, %, status, watts, governor/boost read-only).
4. **Log**: filter chips All / Changes / Warnings / Daemon, search, Follow
   toggle with "Jump to latest", copy visible, rows built with
   `createElement`/`textContent` (never `innerHTML` with data), NUL-safe,
   level chips in the level palette.

**Fan card**: decision panel from `state` ("Level 7 · 4000 RPM", "Step 4 of 5
(AC curve) · since 13:37 (2 m 10 s)", "Up to Max at 80 °C · down to 6 below
72 °C", or the hold/critical/firmware variants, "(estimated)" when `state` is
null), a 36 px mini step sparkline, the **hold grid** (0–7, FW, MAX), a
duration segmented control (5 / 15 / 30 min / "Until I resume" → 0) persisted
in `ui.hold_default_seconds`, a draining progress bar and "+5 min" while holding,
a full-width "Resume curve" button, and the footnote "Critical protection
always applies during a hold." Level 0 is disabled with a tooltip when the
control temp ≥ 55 °C. When the daemon is down the grid still works (legacy
path) but a red notice and "Start daemon" button appear.

**Curve editor**: `draft` is the only state; a single `render(draft)` draws the
SVG and the table. SVG: x = 30..105 °C, y = ordinal rows Firmware, 0..7, Max;
staircase with risers, handles on steps ≥ 1 (base handle moves vertically
only), release ticks/bands from `down`/hysteresis, red critical line with
hatched region, live temp marker, the daemon's active step (`state.step_index`)
emphasised with an "up at / down below" label, ghost of the other curve
(toggle). Dragging with Pointer Events + `setPointerCapture` (fallback to
window listeners): temp clamped between neighbours, Shift snaps to 5, Esc
cancels; keyboard on handles (`role=slider`, arrows ±1/±5, Up/Down level,
Delete removes). Table columns: #, From °C, Level, Release °C (editable `down`;
blank = hysteresis default), RPM (measured from `rpm_by_level`, else `~` static
estimate), ×. Add step (double-click plot or button, max 8), remove (min 1,
never the base). Typing in a table input must **not** re-render the input being
edited (update draft + dirty flag on `input`; full render on `change`/blur or
structural edits). Validation strip: hard errors (non-increasing temps, out of
range, auto above base, rank decreasing, fan-off ≥ 60 °C, `down` out of range)
disable Save; soft warnings (step closer than hysteresis, top step below
critical−10, curve never reaches Max) allow it. Footer: AC/Battery tabs, "Use
battery curve" switch, ghost toggle, dirty indicator, Restore defaults,
Discard, Save. Save posts the whole draft (curve + Response fields) and shows
inline any `rejected` keys; after success the header shows "Applying…" until
`status.config_mtime` catches up. `loadConfig()` retries with backoff; when the
draft is clean and `config_mtime` changes, reload silently; when dirty, show
"config changed on disk — Discard to load it".

**History chart**: two canvases (data + hover overlay), DPR-scaled, series CPU
/ iGPU / Fan RPM (right axis) / TDP (off by default), a 16 px fan-level band
under the plot with block labels, time axis HH:MM, 5/15/30 min range persisted
in `ui.history_range_min`, seeded from `/api/history` then appended from each
status poll (dedupe by `t`), gaps > 3× sample interval break lines and hatch
the band, current step's up/down thresholds and critical drawn as lines, event
markers (alert triangles, override brackets, config dots), hover crosshair +
tooltip with all values, empty state "Collecting samples… (n)", unreachable
state keeps the last data at 60 % with "Not updating".

**Degraded states** are explicit (no page dimming): backend unreachable
(controls disabled, values marked stale), daemon inactive, daemon stale (dot
amber), `state` absent (estimate locally with a JS port of the up/down rules,
label "(estimated)"), fan control unavailable (`fan_control_available`
false — never infer it from `status: disabled`), SMU unavailable (Power view
placeholder + Retry), sensor missing, critical, save failure inline.

**Keyboard**: `0–7` hold at that level with the selected duration, `M` hold
Max, `F` hold Firmware auto, `R` resume curve, `?` shortcut overlay, `Esc`
closes, `Ctrl+1..4` views. Ignore `e.repeat`, ignore while focus is in an
input/select/textarea or on a curve handle, one in-flight action at a time
(`busy` flag). Toasts: a queue of up to 3, 2.6 s each, confirmations only;
failures also render inline where the action lives.

## 18. Installer (`install.sh`) and docs

Idempotent upgrade, in this order:

0. Preflight (abort before touching anything): `python3 -m py_compile` on the
   daemon and fanlib; `systemd-analyze verify` on the unit (copy to a temp dir
   with the final name); `visudo -cf` on the generated sudoers; the repo
   `alert.wav` exists.
1. Backup everything replaced under `/var/backups/thinkpad-fan-control/<stamp>/`.
2. Install binaries under the **same names** (`thinkpad-fan-controld`,
   `fan-set-level.sh`, `fan-config-save.sh`, `ryzenadj-set-tdp.sh`); install
   `alert.wav` to `/usr/local/share/thinkpad-fan-control/`.
3. Write `daemon.env` (§2) from `getent passwd "$TARGET_USER"`, preserving any
   existing values.
4. Config migration: `/usr/local/bin/thinkpad-fan-controld --save-config <
   "$CONFIG"` (or `echo '{}' | …` on first install) so the daemon normalises
   and adds new keys.
5. Log: if larger than 2 MiB, `mv` to `.1`, strip NULs with `tr -d '\000'`,
   gzip; `install -m 0644 /dev/null "$LOG"`; install the logrotate snippet.
6. Sudoers from the template with the user substituted; `visudo -cf` then move.
7. Unit install + `daemon-reload`; `systemctl enable`; restart only if the
   daemon binary or the unit changed (`cmp` against the backup), otherwise
   leave it running. Print that a running manual hold is dropped by a restart.
8. Post-check: within 10 s the unit is active and `state.json` has a fresh `ts`;
   otherwise show `journalctl -u … -n 20`, restore the backed-up daemon + unit,
   restart, and exit 1.
9. Desktop entries: rewrite `~/Desktop/Fan Control.desktop`; for
   `~/.config/autostart/fan-control.desktop` **preserve** the existing
   `X-GNOME-Autostart-enabled` and `Hidden` values (only a first install writes
   `true`).
10. If `pgrep -u "$TARGET_USER" -f fan-gui/app.py` matches, print "Quit and
    relaunch Fan Control to pick up the new client".

`uninstall.sh`: stops/disables the unit, removes the installed files, sudoers,
logrotate snippet and desktop entries; keeps `/etc/thinkpad-fan-control` and the
log unless `--purge`.

`README.md`: rewrite for v2 (architecture, install/upgrade, how the controller
decides with the real parameters and the measured RPM table, hold semantics
and safety envelope, power limits and the freeze-warning wording, simulator
usage, troubleshooting including the watchdog; explain that `status: disabled`
in `/proc/acpi/ibm/fan` only means the fan is stopped at level 0, and that a
missing `commands:` line is what indicates `fan_control=1` is not active).
`launch.sh`: `python3 app.py "$@"` from the script's directory.
