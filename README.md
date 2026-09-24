# ThinkPad Fan Control

Fan, temperature and CPU power-limit control for a ThinkPad T495 (Ryzen 5 PRO
3500U "Picasso", one physical fan that `thinkpad_acpi` exposes as fan1 and
fan2 with identical values) on Linux Mint 22.3. This is version **2.0.0**.
Every schema, path and behaviour is specified in
[`docs/CONTRACT.md`](docs/CONTRACT.md); this README explains it in prose. When
the two disagree, the contract wins.

There are two parts:

* **`thinkpad-fan-controld`** is a root daemon under systemd. It reads the die
  temperature, filters it, walks a fan curve, writes `/proc/acpi/ibm/fan`,
  keeps the EC watchdog armed, plays the critical alert and publishes what it
  is doing.
* **Fan Control** is the dashboard. `app.py` is a GTK window with a tray icon
  and an embedded WebKit view. `server.py` serves the same page to a normal
  browser. Both are built on `fanlib.py`, which reads the sensors, talks to
  the daemon, runs the power-limit (TDP) features and serves the HTTP API.

Either part works without the other. The daemon needs no GUI. Without the
daemon, the GUI can still read sensors and set the fan by hand, and it shows
a red "no thermal protection" warning while it does.

Contents: [1 Pieces](#1-the-pieces-and-how-they-talk) ·
[2 Install](#2-install-upgrade-uninstall) · [3 Dashboard](#3-running-the-dashboard) ·
[4 Controller](#4-how-the-controller-decides) · [5 Holds](#5-holds-and-the-safety-envelope) ·
[6 Power](#6-power-limits-tdp-and-vrm-current) · [7 Simulator](#7-the-simulator) ·
[8 Keys](#8-keyboard-shortcuts) · [9 Files](#9-where-things-live) ·
[10 Troubleshooting](#10-troubleshooting) · [11 Tests](#11-offline-tests)

---

## 1. The pieces and how they talk

```
                 /etc/thinkpad-fan-control/config.json   (curve, response, alerts)
                     ▲ written only by the daemon (--save-config)   │ hot-reloaded (mtime)
                     │                                              ▼
 ┌────────────────────┐  sudo fan-config-save.sh      ┌──────────────────────────┐
 │ Fan Control GUI    │ ────────────────────────────▶ │ thinkpad-fan-controld    │
 │ app.py / server.py │  control.sock (JSON lines):   │ root, systemd Type=notify│
 │ + fanlib.py        │ ◀───── hold · resume ──────▶  │                          │
 │ index.html served  │        status · test_alert    │ k10temp + amdgpu ─▶ curve│
 │ on 127.0.0.1:7070  │ ◀── state.json (every tick)── │ ─▶ /proc/acpi/ibm/fan    │
 │                    │ ◀── history.json (10 s) ───── │    + EC watchdog         │
 │                    │ ◀── /var/log/…log (tail) ──── │                          │
 └────────────────────┘                               └──────────────────────────┘
      │ sudo ryzenadj-set-tdp.sh / sudo ryzenadj --info   (SMU power limits)
      │ sudo fan-set-level.sh                             (only while the daemon is down)
      ▼ sudo systemctl start|stop|restart thinkpad-fan-control.service
```

| Channel | Direction | What it carries |
| --- | --- | --- |
| `/run/thinkpad-fan-control/state.json` | daemon → GUI | A snapshot written every sample (1 s by default) and right after any change: raw, fast and slow temperatures, the commanded level, the level and RPM the EC reports, why the fan is where it is (`reason`), the curve step, hold and critical state, learned RPM per level, whether fan control is available. The daemon counts as **active** when this file parses and its `ts` is less than `3 × sample_interval + 2` seconds old. |
| `/run/thinkpad-fan-control/history.json` | daemon → GUI | The last 30 minutes of samples (at most 1800) and the last 200 events (level changes, critical, holds, config reloads, power-source switches, sensor loss, alerts). It seeds the chart. Written every 10 s and after each event. |
| `/run/thinkpad-fan-control/control.sock` | GUI ↔ daemon | A Unix socket, `root:<your gid>`, mode 0660. The daemon also checks the caller's uid (root or you). One JSON object goes in and one JSON line comes back: `hold`, `resume`, `status`, `test_alert`. A hold is applied before the reply is sent. |
| `config.json` | GUI → daemon | The GUI posts a partial config. `sudo fan-config-save.sh` passes it to `thinkpad-fan-controld --save-config`, which validates it, writes it atomically and reports which keys it rejected. The running daemon notices the new mtime on its next tick. |
| `/var/log/thinkpad-fan-control.log` | daemon → GUI | Plain text, one line per event. The Log view tails it. |
| sudo wrappers | GUI → root | `fan-set-level.sh` (only while the daemon is down), `fan-config-save.sh`, `ryzenadj-set-tdp.sh`, `ryzenadj --info`, and `systemctl start/stop/restart` of the unit. The sudoers file allows nothing else. |

systemd creates everything under `/run/thinkpad-fan-control/`
(`RuntimeDirectory=`) and deletes it when the daemon stops, so a leftover file
never looks like a running daemon. A hold is kept only in the daemon's
memory, so a daemon restart or a reboot drops it.

The dashboard's HTTP API (`fanlib.py`, [§14 of the contract](docs/CONTRACT.md)) is
small:

| GET | Returns |
| --- | --- |
| `/`, `/index.html` | the dashboard |
| `/api/status` | everything the dashboard shows: temperatures, fan, `mode`, daemon state, SMU/TDP readouts, battery |
| `/api/config` | the sanitized `config.json` |
| `/api/history?minutes=N` | up to 30 min of samples and events, with TDP/power merged in from the backend's own sampler |
| `/api/log?lines=N` | the last N log lines (default 200, max 2000), NUL-stripped |
| `/api/settings` | the per-user settings |

| POST | Does |
| --- | --- |
| `/api/fan/hold` `{"level","seconds"}` | hold through the daemon; when the daemon is down, the fallback described in [§5](#5-holds-and-the-safety-envelope) |
| `/api/fan/resume` | end any hold |
| `/api/fan/set` `{"level"}` | old name for hold, using the default hold duration |
| `/api/daemon/start\|stop\|restart` | `sudo -n systemctl …` (Advanced section of the dashboard) |
| `/api/tdp/set`, `/api/tdp/lock`, `/api/tdp/vrm_unlock`, `/api/tdp/restore_stock` | power limits ([§6](#6-power-limits-tdp-and-vrm-current)) |
| `/api/config` | save a partial config through the daemon; the reply includes `rejected` |
| `/api/settings` | save `ui.*`, `apply_tdp_at_startup`, `autostart_enabled` |
| `/api/alert/test` | play the alert (limited to one every 10 s by the daemon) |

---

## 2. Install, upgrade, uninstall

```bash
sudo ./install.sh                 # first install, and every upgrade
sudo ./install.sh --user NAME     # if $SUDO_USER is not the desktop user
sudo ./uninstall.sh               # keeps config and logs
sudo ./uninstall.sh --purge       # removes those too
```

### What `install.sh` does

The installer is safe to run repeatedly. Running it over v1, over v2, or twice
in a row all work, and it restarts the daemon only when something the daemon
loads has changed. The steps run in this order:

0. **Preflight.** Nothing on the system changes until this step passes. The
   installer copies the files it will install to a private staging directory,
   so the files it checks are the files it installs. It then:
   byte-compiles the daemon, `fanlib.py`, `app.py` and `server.py`; checks that
   the daemon declares a `2.x` `VERSION`; runs `bash -n` on the root wrappers;
   runs `systemd-analyze verify` on the unit; renders the sudoers file for your
   user and checks it with `visudo -c`, **and** checks that every rule names
   only the contract's commands (visudo alone would accept `NOPASSWD: ALL`);
   checks that `alert.wav` is a WAV file; and reads `/proc/acpi/ibm/fan` to see
   whether fan control is available now (read only).
1. **Backups.** Every file the run replaces is first copied to
   `/var/backups/thinkpad-fan-control/<stamp>/<original path>`. If no file in
   `/etc/modprobe.d/` and nothing on the kernel command line sets
   `thinkpad_acpi fan_control=1`, the installer adds it to
   `/etc/modprobe.d/thinkpad_acpi.conf` under a marker comment.
2. **Binaries.** `thinkpad-fan-controld`, `fan-set-level.sh`,
   `fan-config-save.sh` and `ryzenadj-set-tdp.sh` go into `/usr/local/bin`
   under the same names as before, so an old GUI that is still running keeps
   working. The fallback alert sound goes to
   `/usr/local/share/thinkpad-fan-control/alert.wav`.
3. **`daemon.env`.** Your uid, gid, home and runtime directory, plus the TDP
   ceilings. Values already in the file are kept. Invalid values and unknown
   keys are reported and replaced (the old file stays in the backup).
4. **Config migration.** The *new* daemon rewrites `config.json` through
   `--save-config`, since it is the only program allowed to write that file.
   A v1 config comes out as schema 2: `poll_interval` is dropped, a disabled
   watchdog becomes 60 s, curves equal to the v1 defaults become the v2
   defaults, and new keys are added. Every other value is kept as it was,
   including v1's `hysteresis` of 5 (the v2 default is 6); change it in the
   dashboard's Response card if you want the new default. The resulting
   curve and any rejected keys are printed. On a first install the default
   config is written.
5. **Log.** A log larger than 2 MiB is rotated once: it becomes `.1` with NUL
   bytes from old crashes removed and is then gzipped (an existing `.1.gz`
   moves to `.2.gz`). The logrotate snippet is installed. The daemon opens
   the log for each line, so it needs no signal.
6. **sudoers.** The rendered file is checked with `visudo` again and moved
   into place in one step.
7. **Unit.** The unit is installed, then `daemon-reload` and `enable` run.
   The daemon is restarted only if its binary, the unit or `daemon.env`
   changed, or if the running daemon reports a different version or no fresh
   `state.json`. Otherwise it keeps running. A restart drops any active
   manual hold; the installer says so, and names the hold when there is one.
8. **Post-check.** Within 10 s the unit must be active and `state.json` must
   have a timestamp newer than the restart. If not, the installer prints the
   last 20 journal lines and the log tail, restores every file this run
   replaced (and removes files it created), returns the service to its
   previous state (restarting the previous daemon if it had been running),
   and exits 1. The same rollback runs if any earlier step fails or the run
   is interrupted. Only the one-time log rotation is not undone, and it loses
   no lines.
9. **Desktop entries.** `~/Desktop/Fan Control.desktop` (only if `~/Desktop`
   exists) and `~/.config/autostart/fan-control.desktop`, which starts the app
   in the tray. Both are written **as you**, not as root, and only when their
   content changes. If the autostart entry exists, its
   `X-GNOME-Autostart-enabled` and `Hidden` values are **kept**, so turning
   autostart off survives upgrades. Only a first install writes `true`. If
   you deleted the entry, an upgrade recreates it switched off, so the
   dashboard's autostart switch still has a file to change.
10. **Running GUI.** If a Fan Control `app.py` or `server.py` from this
    checkout is running, the installer prints *Quit and relaunch Fan Control
    to pick up the new client*.

**REBOOT REQUIRED.** If `thinkpad_acpi` is loaded but `/proc/acpi/ibm/fan`
has no `commands: level …` line, the module was loaded without
`fan_control=1` and nothing can write the fan until it is reloaded with that
option. The installer then installs and enables everything, does **not**
start the daemon (its self-test could not pass), and ends with
**REBOOT REQUIRED**. The modprobe option takes effect at the next boot.
`status: disabled` in that file does **not** mean this; see
[Troubleshooting](#10-troubleshooting).

The installer never writes to `/proc/acpi/ibm/fan`. Only the daemon does,
after its own self-test. The installer also warns if your account has a
`NOPASSWD: ALL` sudo rule from outside this project, because with such a rule
the narrow fan-control rules protect nothing.

### What `uninstall.sh` does

1. It removes the sudoers rule first, so a GUI running without the daemon
   cannot put the fan back to a fixed level through `sudo fan-set-level.sh`.
2. It disables and stops the unit. The daemon's shutdown gives the fan back
   to the firmware.
3. It runs `fan-set-level.sh auto 0` through the installed wrapper **before
   removing it**. This puts the fan back on `auto` even if something had
   fixed it while the daemon was down, and turns off the EC watchdog. If
   `/proc/acpi/ibm/fan` has no `commands:` line there is nothing to undo, and
   this step is skipped.
4. It removes the binaries, the alert sound, the unit, the logrotate snippet
   and both desktop entries (desktop entries are removed as you).

`/etc/thinkpad-fan-control` (config and `daemon.env`) and the log are kept
unless you pass `--purge`. `--purge` also removes the learned RPM table, your
`~/.config/thinkpad-fan-control` settings and
`/etc/modprobe.d/thinkpad_acpi.conf`. That last file is removed only if
`install.sh` created it (marker comment) and it contains nothing else.
Backups are always kept. The uninstaller asks for confirmation; `--yes`
skips the question. You can re-run it safely: a finished uninstall leaves the
fan alone on a second run. `install.sh` and `uninstall.sh` share a lock, so
they never run at the same time.

---

## 3. Running the dashboard

```bash
./launch.sh                  # window (runs python3 app.py from the checkout)
./launch.sh --tray           # start hidden in the tray (the autostart entry passes --tray too)
./launch.sh --port 7071      # another port for the local API
python3 server.py            # browser mode: http://127.0.0.1:7070 (--no-browser, --port N)
```

Closing the window leaves the app running in the tray; quit it from the tray
menu. The GTK app allows only one instance, so launching it again brings the
existing window to the front. If no AppIndicator library is installed,
`--tray` is ignored, the window opens, and closing it quits the app.

The tray shows the control temperature as its label (` 78°`). Its menu has a
status line (`78 °C · 4000 RPM · Curve`, or `Hold 3 · 12:34 left`), *Open
dashboard*, *Hold fan at…* (levels 0–7, *Firmware auto* and *Maximum*, each
for the default hold duration shown in the label), *Resume auto curve*,
*Restore stock power limits* (shown only while VRM current is unlocked) and
*Quit*. You get a desktop notification when the daemon enters critical and
another when a hold ends by itself.

Quitting cleanly (tray *Quit*, Ctrl+C, SIGTERM) undoes what this session did:
a hold this app started is resumed, a fan level set without the daemon goes
back to `auto`, and a VRM unlock is reverted to stock.

The HTTP API listens on `127.0.0.1:7070` only. It rejects requests whose
`Host`, `Origin` or `Sec-Fetch-Site` do not match its own address, which stops
web pages (CSRF, DNS rebinding) from using it. It has **no login**, though:
any program on this machine that can open a TCP connection to 127.0.0.1:7070
can use it, with your sudo rules behind it (fan holds, TDP, daemon
start/stop). On a single-user laptop that is the same trust boundary as your
own account. Do not run the GUI on a machine shared with users you do not
trust, and turn autostart off if you want the API running only while the
dashboard is open.

---

## 4. How the controller decides

### Inputs

* **Control temperature** = `max(k10temp Tctl, amdgpu edge)`, read as
  floats. Both sensors are on the same die, but CPU alone misses GPU-bound
  loads. A reading outside 0–150 °C is invalid. Sensors are found by their
  hwmon **name**, never by `hwmonN` number, because the numbers change between
  boots.
* **Power source** from `/sys/class/power_supply/AC/online` (a missing file
  counts as AC). The curve switches only after 3 identical readings in a row.
* **The EC** via `/proc/acpi/ibm/fan`: the level it reports, the fan speed
  (`speed:`), the `status:` word, and whether a `commands: level …` line is
  present, meaning fan control is available.

### Why there are two filters

On this APU, Tctl can jump by up to 11 °C within one second, in bursts
that last only a few seconds. A controller that reacts to the raw value flaps between
level 7 and disengaged, which is what v1 did: 4000 ↔ 6400 RPM about once a
minute under a one-core load. v2 keeps two exponential moving averages (EMAs)
of the same signal:

| Filter | Time constant | Used for |
| --- | --- | --- |
| `T_fast` | `smoothing_up_s` = 8 s | stepping **up**, so the fan reacts to real heating within seconds |
| `T_slow` | `smoothing_down_s` = 30 s | stepping **down**, so a short dip never releases a step |

Both filters start from the first valid reading. A config reload never resets
them.

### The curves and the measured RPM

A curve has 1–8 steps `{temp, level, down?}`. Step 0 always starts at 0 °C.
These are the defaults. **RPM** is what this unit's EC reports at that level;
only two values have been measured by hand:

**AC curve** (`curve`)

| Step | Up when `T_fast ≥` | Level | Down when `T_slow <` | RPM on this T495 |
| --- | --- | --- | --- | --- |
| 0 | — | `auto` (firmware curve) | — | firmware decides |
| 1 | 50 °C | `4` | 44 °C (50 − hysteresis 6) | learned |
| 2 | 60 °C | `6` | 54 °C | learned |
| 3 | 70 °C | `7` | 64 °C | **≈ 4000 (measured)** |
| 4 | 80 °C | `disengaged` (maximum) | **72 °C** (explicit `down`) | **≈ 6400 (measured)** |

**Battery curve** (`battery_curve`, used while on battery if
`use_battery_curve` is true)

| Step | Up when `T_fast ≥` | Level | Down when `T_slow <` |
| --- | --- | --- | --- |
| 0 | — | `auto` | — |
| 1 | 60 °C | `3` | 54 °C |
| 2 | 70 °C | `5` | 64 °C |
| 3 | 80 °C | `7` | 74 °C |
| 4 | 87 °C | `disengaged` | **78 °C** (explicit) |

A step's release temperature is its explicit `down` if it has one, otherwise
`temp − hysteresis` (default 6), computed when needed. The top step gets a
wider explicit release because of the 4000 → 6400 RPM gap between level 7 and
disengaged. Under a steady load the die can sit above 80 °C at level 7 and
fall below 75 °C at disengaged, so a 6 °C band there cannot settle. That gap
is the whole "top band" problem.

`disengaged` takes the fan out of EC regulation: it runs as fast as it can
(about 6400 RPM here, far above level 7's 4000). `full-speed` is another name
for the same thing and ranks the same. Level `0` stops the fan.

RPM per level is **learned**, not hard-coded. After 20 s at a commanded
level, the daemon adds the EC speed to a list of the last 50 readings for
that level. The median is published as `state.rpm_by_level`, shown in the
curve editor and hold grid, and saved in `/var/lib/thinkpad-fan-control/rpm.json`
when it changes by 50 RPM or more. Levels the fan has never sat at show a `~`
estimate until they are measured.

### Each tick (every `sample_interval`, default 1 s)

1. **Sensor loss.** After three invalid readings in a row, the fan goes to
   `auto` (or stays `disengaged` if critical is active), one log line is
   written, and `reason` becomes `sensor_lost`. When readings come back, both
   filters restart from the new reading and the curve is re-seated.
2. **Filters** update.
3. **Power source.** When the 3-reading check confirms a switch, the log says
   `Power -> battery|AC` and the curve is re-seated.
4. **Config reload** if `config.json`'s mtime changed. An unreadable or
   invalid file keeps the running config and is logged once. Filters and
   holds are never reset by a reload.
5. **Critical.** Entered when the raw temperature is at or above
   `critical_temp` (90) on **two samples in a row**, or at or above 93 once.
   The fan goes to `disengaged` regardless of the curve or a hold. The alert
   sound plays (again every `alert_cooldown` = 300 s while critical lasts, as
   `ALERT: critical since HH:MM:SS (raw …, slow …)`), and the GUI shows a
   desktop notification. Critical clears only when `T_slow < 90 − 8 = 82`
   **and** at least `critical_exit_hold_s` = 60 s have passed. The fan then
   goes to the **top curve step** with fresh dwell timers, and the normal
   down rules bring it down one step at a time. (Jumping straight to the
   step matching `T_fast` dropped to level 7, reheated and re-entered
   critical in simulation.)
6. **Curve.** This is always computed, even while a hold or critical is in
   control, so `curve_level` always shows what the curve would do.
   *UP*: while `T_fast ≥` the next step's temperature, move up one step per
   tick. *DOWN*: move down one step when `T_slow` is below the current step's
   release temperature **and** at least `dwell_down_s` = 45 s have passed
   since the last UP **and** at least `step_down_spacing_s` = 10 s have
   passed since the last change. So after a heat burst pushes the fan up, it
   stays at that level for at least 45 s, and a long cool-down steps down
   every 10 s instead of dropping straight to auto.
7. **Target.** The first match wins: critical → `disengaged`; an active,
   unsuspended hold → its level; otherwise the curve level.
8. **Hold expiry.** The hold is cleared, the log says `Override <level> ended
   (expired)`, and the curve is re-seated.
9. **Fan write.** The daemon writes only when the level changes or the
   refresh interval has passed (`min(20, watchdog / 3)` s, so 20 s by
   default). Each change is one log line:
   `Fan 7 -> disengaged (raw 81.2 fast 80.1 slow 77.9 | step 4/5 ac | up>=80 down<72) [curve]`.
   If the EC reports a level the daemon did not command (other than the
   watchdog's `auto`), the daemon logs `external fan write detected (level X)`
   once and writes its own level again.
10. **EC watchdog.** `watchdog 60` by default. If nothing writes the fan for
    that long (crash, SIGKILL, a hung machine), the embedded controller
    returns the fan to firmware control on its own. A non-zero value is never
    below `3 × sample_interval + 15`, and the refresh in step 9 keeps it from
    firing while the daemon is healthy. `0` turns this safety net off; the
    dashboard offers Off / 60 / 90 / 120.
11. **State.** `state.json` is written every tick; `history.json` every 10 s
    and after events.

**Re-seating** (at start, after a config reload, an AC switch, sensor
recovery, a hold ending, or *Resume*) jumps to the highest step whose
temperature is at or below `T_fast` and restarts the dwell timer.
Critical exit is the exception described in step 5.

### Start, stop, supervision

The daemon waits up to 60 s (in 2 s steps) for `/proc/acpi/ibm/fan` and a
readable sensor, then runs a **self-test**: it reads the current level and
writes it back. If that write fails (for example `thinkpad_acpi` without
`fan_control=1`, or a sandbox change that made the file read-only), it logs a
`FATAL:` line and exits 1 instead of claiming to control a fan it cannot
touch. Only then does it tell systemd it is ready (`Type=notify`). It pings
systemd's watchdog (`WatchdogSec=45`) from its main loop, so a hung daemon is
killed and restarted. On SIGTERM it logs `Daemon stopping — returning fan to
firmware control`, writes `watchdog 0` then `level auto`, and exits 0.
`Restart=on-failure` with a limit of 5 starts in 300 s means a daemon that
keeps crashing stops being restarted instead of filling the log.

### Configuration reference (`config.json`, schema 2)

| Key | Default | Allowed | Meaning |
| --- | --- | --- | --- |
| `sample_interval` | 1 | 1–5 s | tick period |
| `smoothing_up_s` | 8 | 2–30 s | time constant of `T_fast` |
| `smoothing_down_s` | 30 | 5–120 s | time constant of `T_slow` |
| `dwell_down_s` | 45 | 0–300 s | minimum time after an UP before any DOWN |
| `step_down_spacing_s` | 10 | 0–60 s | minimum time between two DOWN steps |
| `hysteresis` | 6 | 0–20 °C | release gap for steps without `down` |
| `critical_temp` | 90 | 70–105 °C | critical threshold (raw temperature) |
| `critical_exit_margin` | 8 | 3–20 °C | leave critical when `T_slow < critical_temp − margin` |
| `critical_exit_hold_s` | 60 | 10–600 s | minimum time in critical |
| `watchdog` | 60 | 0, or 15–120 s | EC watchdog; a non-zero value is raised to at least `3·sample_interval + 15` |
| `override_max_seconds` | 7200 | 60–14400 s | longest hold; "Until I resume" uses this |
| `override_ceiling_temp` | `critical_temp − 8` (82) | 60 … `critical_temp − 1` | hold ceiling ([§5](#5-holds-and-the-safety-envelope)) |
| `curve`, `battery_curve` | see above | 1–8 steps | `{"temp", "level", "down"?}` |
| `use_battery_curve` | true | bool | |
| `alerts_enabled` | true | bool | |
| `alert_sound` | `/home/jhnlstrlclcn/Music/SYSTEM SOUND/90c.mp3` | absolute path, at most 400 characters | if unreadable when needed, the installed `alert.wav` plays |
| `alert_cooldown` | 300 | 30–3600 s | time between repeat alerts |

Curve rules the daemon enforces (an invalid curve is rejected as a whole and
the previous one kept): temperatures strictly increasing within 0–105 (step 0
is forced to 0); `auto` only at step 0; the fan level never decreases as
temperature rises; no level `0` at 60 °C or above; `down` between
`temp − 20` and `temp − 1`.

Every value the GUI sends is checked by the root daemon (`--save-config`),
not by the GUI. A value that is out of range or the wrong type is **not**
clamped; the key keeps its previous value and is listed under `rejected` in
the reply, which the curve editor shows next to the field. Unknown keys are
dropped and reported the same way. The only adjustment the daemon makes
deliberately is raising a non-zero `watchdog` to the minimum above. The
dashboard's Response card has smoothing presets Off / Light / Normal / Heavy,
which set `(smoothing_up_s, smoothing_down_s)` to (2, 5) / (4, 15) / (8, 30) /
(12, 60).

---

## 5. Holds and the safety envelope

A **hold** keeps the fan at one level for a set time: `0`–`7`, *Firmware*
(`auto`) or *Max* (`disengaged`), for 5 / 15 / 30 min or *Until I resume*
(= `override_max_seconds`, 2 h by default). You can start one from the Fan
card grid, the tray or the keyboard. *+5 min* extends a running hold, and
**Resume curve** ends it at once and re-seats the curve on the current
temperature.

The daemon keeps watching the temperature during a hold. That is why holds go
through the daemon instead of stopping it:

| Rule | What happens |
| --- | --- |
| **Critical always wins** | At critical the fan goes to `disengaged` whatever the hold says. |
| **Ceiling** | If `T_fast ≥ override_ceiling_temp` (82 °C) **and** the curve wants more airflow than the hold, the hold is **suspended**: mode `hold_suspended`, the curve level is used, one log line. It resumes only when `T_slow < 82 − hysteresis = 76 °C` **and** it has been suspended for at least 30 s. A single Tctl spike therefore cannot flip the fan back and forth. Holding *Max* is never suspended because nothing gives more airflow. Holding *Firmware* is not suspended either: the EC's own curve is running, and critical still overrides it. |
| **No fan-off when warm** | A hold at `0` is refused (`too_hot_for_fan_off`) while the raw temperature is 55 °C or more, or unknown. The button is disabled with a tooltip. |
| **Fan control unavailable** | A hold is refused (`fan_control_unavailable`) when `/proc/acpi/ibm/fan` has no `commands: level …` line, which means `thinkpad_acpi` is loaded without `fan_control=1`. It is **never** refused because of `status: disabled`, which only means the fan is at level 0 right now. |
| **Bounded** | Every hold expires. The GUI shows the countdown and notifies you when a hold ends by itself. |
| **Volatile** | Holds live in the daemon's memory. A daemon restart (including one by `install.sh`) or a reboot drops them. |
| **Watchdog stays armed** | If the daemon dies during a hold, the EC still returns to `auto`. |

**Without the daemon** the hold grid still works, through `sudo
fan-set-level.sh <level> 120`: the EC watchdog is set to 120 s before every
write, so a GUI that is killed hands the fan back within two minutes. While
the GUI runs, a keep-alive thread rewrites the level every 30 s and checks
the temperature every 2 s. At or above `critical_temp` (one sample is enough
on this path) it writes `disengaged` instead (`manual_critical`). After the
temperature has stayed below `critical_temp − critical_exit_margin` for 10 s
and the critical hold time has passed, it hands the fan to firmware `auto`
rather than back to the level that could not keep up. Three unreadable
sensor samples, the hold expiring, *Resume* or quitting the app end it with
`auto`; when the daemon comes back, the keep-alive stops and the daemon takes
over. The dashboard calls this mode
`manual_unprotected` and shows a red "no thermal protection" notice with a
**Start daemon** button: there is no curve, no filtering and no ceiling on
this path.

Modes in the status pill (and the tray): `curve`, `hold`, `hold_suspended`,
`critical`, `sensor_lost`, and with the daemon down, `firmware` (fan on
`auto`) or `manual_unprotected`.

---

## 6. Power limits (TDP and VRM current)

The 3500U is a 15 W part with a configurable range of 12–25 W. The Power view
drives `ryzenadj` through `ryzenadj-set-tdp.sh <watts> <vrm>`:

* **Slider** from 5 W to `FANCTL_TDP_MAX`, in zones: up to 15 W stock
  (teal), 16–25 W raised (amber), above 25 W beyond spec (red). Nothing is
  applied until you press **Apply**. Presets are Quiet 12 / Stock 15 /
  Boost 25 / Last used. The "Requested vs SMU reports" line shows when the
  firmware has quietly changed the limit back.
* **Ceilings** come from `/etc/thinkpad-fan-control/daemon.env`:
  `FANCTL_TDP_MAX=35` and `FANCTL_TDP_MAX_VRM_UNLOCKED=30`. The root wrapper
  refuses (exit 2) anything above them, and accepts only 5–40 written without
  leading zeros (`010` would otherwise be read as octal 8). The backend
  refuses before it even calls the wrapper.
* **VRM current.** Stock is EDC 45 A / TDC 35 A; unlocked is 60 A / 42 A.
  Both use `--tctl-temp=95`. What the UI calls "unlocked" is **read from the
  hardware** (`ryzenadj --info` reports an EDC limit of 55 A or more), not a
  remembered checkbox. Unlocking takes two clicks (arm, then confirm within
  5 s), is never saved, and is reverted to stock when you quit the app or
  press **Restore stock**.
* **Keep limit applied** (`tdp_locked`) re-applies the *requested* watts on a
  5 s cycle when the SMU value drifts by 1 W or more, after a suspend/resume,
  or after a power-source change. It re-applies at most once every 20 s and
  is **paused** while the control temperature is at or above
  `critical_temp − 3` (87 °C), so a TDP re-apply never works against a
  thermal event.
* **Apply at startup** runs only if the previous session exited cleanly and
  the requested value is 25 W or less, and always uses stock VRM current.
  Otherwise the status says why it was skipped (`unclean_exit` or
  `above_25w`).

> **Freeze warning.** When the SMU reports a limit of 30 W or more **and**
> the VRM current is unlocked (`freeze_config_warning` in `/api/status`), the
> TDP readout gets a warning icon and a banner says that this power
> configuration **preceded the 2026-09-15 hang, not proven causal**. That is
> the whole claim: on 2026-09-15 the machine froze with an amdgpu (GPU driver)
> hang during a DaVinci Resolve session while about 30 W and unlocked VRM
> current were applied. Nobody has shown that the power configuration caused
> it. Treat it as a known risky combination, not a forbidden one.
> `FANCTL_TDP_MAX_VRM_UNLOCKED=30` is the hard limit, and **Restore stock**
> (or quitting the app) puts the VRM current back.

The SMU readouts (STAPM limit, PPT fast/slow, EDC, TDC, THM) come from
`sudo ryzenadj --info`, read every 10 s while the window is visible and every
30 s while the app is in the tray. If `ryzenadj` fails, the last good values
are kept and marked stale, `smu.ok` becomes false, and nothing is made up.

---

## 7. The simulator

The daemon's decision code can run against recorded or generated data, with
a fake clock and a fake fan: no root, no hardware, no files under `/run`, no
sound. This is how the defaults were chosen, and `tests/sim/run.sh` uses it
to check them. Run it straight from the checkout:

```bash
D=daemon/thinkpad-fan-controld

# generate a 30-minute trace of a steady 15 W load (CSV on stdout)
python3 $D --gen-trace load15 --seconds 1800 --seed 7 > ~/load15.csv

# replay it through the real controller with the default config
python3 $D --simulate ~/load15.csv

# what-if: shorter dwell, one JSON summary line, no per-transition output
python3 $D --simulate ~/load15.csv --set dwell_down_s=30 --json --quiet

# the same trace under the v1 rules (raw thresholds, 5 °C hysteresis)
python3 $D --simulate ~/load15.csv --set legacy=1 --quiet

# replay what the live daemon saw in the last 30 minutes
python3 $D --simulate /run/thinkpad-fan-control/history.json

# start from your own config instead of the defaults
python3 $D --simulate ~/load15.csv --config /etc/thinkpad-fan-control/config.json
```

| Option | Meaning |
| --- | --- |
| `--gen-trace SCENARIO` | print a generated trace: `idle`, `load12`, `load15`, `load18` (steady loads of that many watts), `stop` (a heavy load that ends, to test the cool-down cascade), `ramp` (a scripted climb through critical to a 92–94 °C plateau and back), `acflip` (real AC changes plus one- and two-sample bounces), `sensorloss`, `override` (a hold at level 3 through a heat burst and a load that pushes past the ceiling) |
| `--seconds N`, `--seed S` | trace length (default 3600, minimum 120) and random seed (default 1); the same seed always gives the same trace |
| `--simulate TRACE` | a CSV trace or a `history.json` |
| `--config FILE` | start from this config instead of the built-in defaults |
| `--set key=value` | change one config key (repeatable; values are parsed as JSON, so `--set watchdog=90`, `--set use_battery_curve=false`); `legacy=1` runs the v1 rules for comparison |
| `--json` | print the summary as one JSON object |
| `--quiet` | summary only, no transition lines |
| `--detail` | with `--json`: include per-tick records, events and log lines |
| `--open-loop` | replay recorded temperatures even if the trace has a power column |

The generator uses a first-order thermal model (`C = 50 J/K`, ambient 36 °C,
thermal resistance from 4.8 K/W at `auto` down to 2.25 K/W at `disengaged`,
limited at 95 °C) plus Tctl bursts like the real ones: +5–14 °C for 1–3 s
about every 25 s under load. Generated traces include the package power
(`p_w`), so they replay **closed loop**: the simulated die temperature
responds to the fan level the controller chooses, and a what-if config
changes the temperatures too. A recorded trace has no power column and
replays **open loop**: the recorded temperatures are used as they are,
whatever the simulated fan does. That is good for counting decisions, not
for judging cooling. `ramp` is scripted and always open loop.

A CSV needs `ts` (or `t_s`) and `cpu` (or `tctl_c`); `igpu` (or `gpu_c`),
`ac` and `sensor_ok` are optional, and other columns are ignored. So
`tests/sim/traces/real-2026-09-23-mini-eq-1hz.csv` (900 s of real 1 Hz data
recorded on this machine under a one-core load) replays directly.

The summary reports `changes_per_hour`, `transitions`, `median_dwell_s`,
`min_dwell_s`, `min_gap_opposite_s` (the shortest 7 → max → 7 style
reversal), `seconds_at_or_above_critical`, `time_share_per_level` and
`alerts`.

`tests/sim/run.sh` generates every scenario for seeds 1–10 (one hour each)
and checks the **mean across the seeds** unless a rule says otherwise:
idle ≤ 10 changes/h; `load15` ≤ 30/h, with no single seed above 40 and no
reversal closer than 10 s on any seed; `load12` ≤ 10/h; `stop` makes its
first DOWN step within 60 s of the load ending and never steps up during the
cascade; `ramp` enters critical within two samples of the second reading at
or above 90 °C, alerts exactly once per cooldown and exits only after the
hold; `acflip` switches once per real change and ignores the bounces;
`sensorloss` goes to `auto` after three missing samples with one log line and
re-seats on recovery; `override` holds through 85 °C, is suspended above the
ceiling, gives way to critical and re-seats on expiry. It also replays the
real trace through the v2 controller and through the v1 rules and requires at
least **3× fewer transitions**. It prints a table and exits non-zero if any
check fails. `SIM_SEEDS="1 2 3"`, `SIM_SECONDS=1800` and `SIM_KEEP=1` (keep
the work directory) change how it runs.

---

## 8. Keyboard shortcuts

In the dashboard:

| Key | Action |
| --- | --- |
| `0` – `7` | hold the fan at that level for the selected duration |
| `M` | hold Max (`disengaged`) |
| `F` | hold Firmware auto |
| `R` | resume the curve |
| `Ctrl+1` … `Ctrl+4` | views: Overview · Fan curve · Power · Log |
| `?` | shortcut overlay |
| `Esc` | close the overlay, cancel a curve-handle drag, or disarm a two-step button (such as VRM unlock or daemon stop/restart) |

Shortcuts are ignored while you type in a field or while a curve handle has
focus. Held-down keys do not repeat an action, and only one action runs at a
time. In the curve editor a focused handle works like a slider: Left/Right
move its temperature by 1 °C (5 °C with Shift), Up/Down change its level, and
Delete removes the step (never the base step). While dragging a handle with
the mouse, Shift snaps to 5 °C steps and Esc cancels the drag.

---

## 9. Where things live

Installed by `install.sh`:

| Path | Owner / mode | Purpose |
| --- | --- | --- |
| `/usr/local/bin/thinkpad-fan-controld` | root 0755 | the daemon (also `--version`, `--save-config`, `--self-test`, `--simulate`, `--gen-trace`) |
| `/usr/local/bin/fan-set-level.sh` | root 0755 | `fan-set-level.sh <level> [watchdog]`: direct EC write, used only while the daemon is not running |
| `/usr/local/bin/fan-config-save.sh` | root 0755 | `exec thinkpad-fan-controld --save-config` |
| `/usr/local/bin/ryzenadj-set-tdp.sh` | root 0755 | `ryzenadj-set-tdp.sh <watts> <vrm>`: TDP / VRM wrapper with the ceilings from `daemon.env` |
| `/etc/thinkpad-fan-control/config.json` | root 0644 | curves and thresholds; written only by the daemon |
| `/etc/thinkpad-fan-control/daemon.env` | root 0644 | `FANCTL_GUI_UID`, `FANCTL_GUI_GID`, `FANCTL_GUI_HOME`, `FANCTL_GUI_RUNTIME_DIR`, `FANCTL_TDP_MAX`, `FANCTL_TDP_MAX_VRM_UNLOCKED` |
| `/etc/systemd/system/thinkpad-fan-control.service` | root 0644 | the unit (`Type=notify`, sandboxed) |
| `/etc/sudoers.d/fan-control` | root 0440 | the wrappers, `ryzenadj --info`, and `systemctl start/stop/restart` of the unit |
| `/etc/logrotate.d/thinkpad-fan-control` | root 0644 | rotate at 2 MB, keep 2, compress |
| `/etc/modprobe.d/thinkpad_acpi.conf` | root 0644 | `options thinkpad_acpi fan_control=1`, written only if nothing else sets it |
| `/run/thinkpad-fan-control/` | root 0755 | created by systemd while the daemon runs: `state.json` (0644), `history.json` (0644), `control.sock` (root:your group, 0660) |
| `/var/lib/thinkpad-fan-control/rpm.json` | root 0644 | learned RPM per level |
| `/var/log/thinkpad-fan-control.log` | root 0644 | the daemon log |
| `/usr/local/share/thinkpad-fan-control/alert.wav` | root 0644 | fallback alert sound |
| `/var/backups/thinkpad-fan-control/<stamp>/` | root | everything an install replaced |
| `~/.config/thinkpad-fan-control/settings.json` | you, 0600 (dir 0700) | GUI settings: requested TDP, lock, apply at startup, hold duration, chart range, log filter, last view |
| `~/Desktop/Fan Control.desktop`, `~/.config/autostart/fan-control.desktop` | you | launcher and autostart entry (tray mode) |

In the checkout:

| File | What it is |
| --- | --- |
| `daemon/thinkpad-fan-controld` | the daemon: standard-library Python only |
| `daemon/thinkpad-fan-control.service`, `daemon/fan-control.sudoers`, `daemon/thinkpad-fan-control.logrotate`, `daemon/daemon.env.example` | files the installer installs or renders |
| `daemon/fan-set-level.sh`, `daemon/fan-config-save.sh`, `ryzenadj-set-tdp.sh` | root wrappers |
| `fanlib.py` | sensors, daemon client, SMU polling, TDP lock, HTTP API |
| `app.py` / `server.py` / `launch.sh` | GTK app with tray / browser mode / launcher |
| `index.html` | the dashboard, one self-contained file |
| `install.sh` / `uninstall.sh` | [§2](#2-install-upgrade-uninstall) |
| `tests/` | [§11](#11-offline-tests) |
| `docs/CONTRACT.md` | the specification |

---

## 10. Troubleshooting

**Start here**

```bash
systemctl status thinkpad-fan-control.service
journalctl -u thinkpad-fan-control.service -n 50 --no-pager
tail -n 50 /var/log/thinkpad-fan-control.log
python3 -m json.tool /run/thinkpad-fan-control/state.json
cat /proc/acpi/ibm/fan
```

**`/proc/acpi/ibm/fan` says `status: disabled`.** This is **not** a problem
and **not** the `fan_control` module option. `thinkpad_acpi` prints
`disabled` whenever the EC fan register is 0, which means the fan is stopped
at level 0 (for this driver, `level 0` and `disable` are the same thing).
You see it after a level-0 hold, a curve step at level 0, or
`echo level 0` / `echo disable` from another tool. Holds, *Resume* and the
curve all keep working. Nothing in v2 (daemon, GUI, installer) treats
`status: disabled` as "fan control off".

**No `commands:` lines in `/proc/acpi/ibm/fan`.** *This* is the real "fan
control off": `thinkpad_acpi` was loaded without `fan_control=1`, so every
write fails. With the option, the file ends with three lines:
`commands: level <level> …`, `commands: enable, disable` and
`commands: watchdog <timeout> …`. Check
`cat /sys/module/thinkpad_acpi/parameters/fan_control` (it should print `Y`).
`install.sh` writes `options thinkpad_acpi fan_control=1` to
`/etc/modprobe.d/thinkpad_acpi.conf` and asks for a reboot. Until then the
daemon's self-test fails (the unit shows `failed` with a `FATAL: self-test
write …` line), holds are refused with `fan_control_unavailable`, and the
dashboard warns that fan control is unavailable. If the lines are still
missing after a reboot, the module may be loaded from the initramfs, which
does not see the new option: check with
`lsinitramfs /boot/initrd.img-$(uname -r) | grep thinkpad_acpi`, and if it is
there, run `sudo update-initramfs -u` and reboot.

**The fan keeps going back to `auto` (firmware control).** This is the EC
watchdog doing its job: nothing wrote to the fan for `watchdog` seconds (60 by
default). Either the daemon is stopped (`systemctl start
thinkpad-fan-control`), or it hung and systemd's `WatchdogSec=45` killed it.
The journal shows that, and `Restart=on-failure` starts it again 5 s later.
Without the daemon, a GUI hold renews the watchdog every 30 s; if the GUI
was killed, the fan returns to `auto` within 120 s, which is intended. If the
log says `external fan write detected (level X)`, another program (thinkfan,
a script, a manual `echo level …`) is writing to `/proc/acpi/ibm/fan`. The
daemon writes its own level again, but two controllers fighting over the fan
is a configuration mistake: use a hold instead of a manual write while the
daemon runs.

**Daemon dot is amber ("stale").** `state.json` exists but its timestamp is
older than `3 × sample_interval + 2` seconds, so the daemon is still
registered but has stopped publishing (blocked, or being killed). The
dashboard keeps the last data, marks it stale and disables controls that need
the daemon. The EC watchdog hands the fan to the firmware if the daemon stays
silent, and systemd's watchdog restarts it. `systemctl status` and the
journal tell you why.

**Daemon dot is red / "Start daemon".** `/run/thinkpad-fan-control/` is gone
because the unit is not running. Run `sudo systemctl start
thinkpad-fan-control`, or use the button in the dashboard's Advanced section.
If it stops again at once, the journal shows why: the self-test `FATAL` (see
the `commands:` entry above), or `FATAL: … not available after 60 s` when
`k10temp`/`amdgpu` never became readable. After 5 failures in 300 s systemd
stops trying; fix the cause, then `sudo systemctl reset-failed
thinkpad-fan-control` and start it again.

**"Port in use — another copy is already running".** Something already owns
`127.0.0.1:7070`, usually the GTK app sitting in the tray. It allows only one
instance, so launching `app.py` again just raises its window, but `server.py`
cannot share the port. Quit the tray app, or use another port:
`python3 server.py --port 7071`. A different message, `Cannot listen on
127.0.0.1:N`, means the port number itself is not usable.

**SMU unavailable / the Power view shows a placeholder.** `sudo ryzenadj
--info` failed or printed a table without `STAPM LIMIT`. Run it yourself to
see the error; `ryzenadj` needs access to the SMU, which kernel lockdown
under Secure Boot can block. The dashboard keeps the last good readout
marked stale and offers *Retry*. It never makes up a value, and the TDP lock
does nothing while `smu.ok` is false.

**"Quit and relaunch Fan Control to pick up the new client".** The installer
found `app.py` or `server.py` from this checkout running. The daemon side is
already upgraded, but the running GUI still uses the old page and library.

**The installer rolled back.** Either a step failed (the line and command
are printed) or the post-check did not see an active unit with a fresh
`state.json` within 10 s; the 20 journal lines it printed show the cause.
Every file the run replaced was restored from
`/var/backups/thinkpad-fan-control/<stamp>/`, files it created were removed,
and the service was returned to its previous state. Fix the cause and run
`sudo ./install.sh` again. A start that hangs is usually the daemon still
waiting (up to 60 s) for `/proc/acpi/ibm/fan` or a readable sensor.

**"REBOOT REQUIRED" at the end of the install.** See the `commands:` entry
above. Everything is installed and enabled; the daemon starts at the next
boot. Until then the firmware drives the fan.

**Autostart stays off after an upgrade.** This is intended: the installer
keeps `X-GNOME-Autostart-enabled` from the existing entry. Turn it on in
Startup Applications or with the dashboard's autostart switch.

**The installer warns about `NOPASSWD: ALL`.** Your account has a sudo rule
(outside this project, for example `/etc/sudoers.d/90-<you>-nopasswd`) that
lets anything running as you become root without a password. While it exists,
the narrow fan-control rules and the wrappers' argument checks protect
nothing. Removing or narrowing that rule is your decision; Fan Control works
either way.

**No alert sound.** The daemon plays `alert_sound` as *your* user through the
PulseAudio/PipeWire socket in `/run/user/<uid>` (from `daemon.env`), trying
`paplay` first and `gst-play-1.0` if that fails. The file must be readable by
you; otherwise the installed `alert.wav` plays. Use **Test** in *Daemon &
alerts* (limited to one every 10 s) and look for a single `WARN` line in the
log. Alerts fire only on a *confirmed* critical entry (two samples in a row
at or above 90 °C, or one at 93 °C or above), so a one-second Tctl spike does
not make a sound.

**Logs.** The daemon log is plain text with one `[YYYY-mm-dd HH:MM:SS]
message` per line. logrotate rotates it at 2 MB and keeps two compressed
generations. Identical warnings are written at most once a minute, with an
`(xN)` count when they repeat. Level changes, config reloads, holds,
critical entry and exit, alerts, power-source switches and sensor loss are
logged; watchdog refreshes and per-sample data are not. Old logs can contain
NUL bytes from hard crashes, and readers strip them. `journalctl -u
thinkpad-fan-control` has the systemd side (start, stop, self-test, watchdog
restarts).

**Sensors show `n/a`.** The hwmon device is missing. `cat
/sys/class/hwmon/*/name` should list `k10temp`, `amdgpu`, `thinkpad`, `nvme`
and `iwlwifi_1`. The controller needs at least one of `k10temp` or `amdgpu`;
NVMe and Wi-Fi are only displayed.

---

## 11. Offline tests

None of these touch the real fan, sudo, systemd, `/run` or `/etc`:

```bash
tests/sim/run.sh                                             # controller acceptance (§7)
python3 -m unittest tests/test_daemon.py                     # daemon logic, config, socket
FANCTL_DRY_RUN=1 python3 -m unittest tests/test_fanlib_api.py -v   # backend API
python3 tests/mock_api.py --port 7150 --scenario hold       # dashboard against a fake backend
```

`tests/test_daemon.py` runs the daemon's decision code on its fake clock and
fake fan, and starts the real binary only with every path redirected to a
temporary directory and a fake fan file. It refuses to run as root, because
the daemon ignores those redirections when it is root.

`FANCTL_DRY_RUN=1` makes the backend skip every sudo, socket and sound action
and report it as successful; sensor reads stay real. Test servers use ports
7100–7199 so they never clash with a running dashboard on 7070.
`tests/mock_api.py` serves `index.html` with simulated data for scenarios
such as `curve`, `hold`, `critical`, `daemon_down`, `manual_unprotected`,
`stale`, `smu_missing`, `sensor_lost`, `freeze` and `battery` (the list is
in the file's header). It is a development aid with its own simplified
controller, not a reference for daemon behaviour.
