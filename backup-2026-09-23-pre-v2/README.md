# ThinkPad Fan Control

Fan, temperature and CPU power-limit control for a ThinkPad T495 (Ryzen 3x00U
"Picasso") running the `thinkpad_acpi` driver.

Two pieces:

* a **root daemon** that drives the fan from a temperature curve, and
* a **GTK dashboard** for watching sensors and taking manual control.

They talk to each other only through `/etc/thinkpad-fan-control/config.json`
and systemd, so either one works without the other running.

---

## Install

```bash
sudo ./install.sh
```

Installs the daemon, the privileged wrappers, sudoers rules, the systemd unit
and the desktop entries. Anything it replaces is copied to
`/var/backups/thinkpad-fan-control/<timestamp>/` first.

## Run

```bash
python3 app.py            # dashboard window
python3 app.py --tray     # start hidden in the tray (this is what autostart uses)
python3 server.py         # browser mode on http://127.0.0.1:7070
```

Closing the window leaves the app in the tray. Quit from the tray menu.

## Files

| Path | What it is |
| --- | --- |
| `fanlib.py` | All hardware and daemon access, plus the HTTP API. Shared by `app.py` and `server.py`. |
| `app.py` | GTK/WebKit window and the tray indicator. |
| `server.py` | Same API and UI, served to a browser instead. |
| `index.html` | The dashboard. Self-contained — no network fetches. |
| `daemon/thinkpad-fan-controld` | The fan curve daemon. Runs as root via systemd. |
| `daemon/fan-set-level.sh` | Whitelists fan levels written to `/proc/acpi/ibm/fan`. |
| `daemon/fan-config-save.sh` | Entry point for saving the config; the daemon re-validates it. |
| `ryzenadj-set-tdp.sh` | Clamps and applies TDP / VRM current limits. |

Runtime state lives outside this directory:

| Path | What it is |
| --- | --- |
| `/etc/thinkpad-fan-control/config.json` | The curve and thresholds. Edit from the GUI. |
| `/var/log/thinkpad-fan-control.log` | Daemon log, shown in the dashboard. |

## The fan curve

The daemon picks a fan level from the hottest of **CPU Tctl (`k10temp`)** and
**iGPU edge (`amdgpu`)** — they share a die on this APU, and CPU alone misses
GPU-bound loads.

Defaults on AC:

| Control temp | Fan level | Roughly |
| --- | --- | --- |
| below 50 °C | `auto` | firmware decides |
| 50 °C | 4 | ~3100 RPM |
| 60 °C | 6 | ~4000 RPM |
| 70 °C | 7 | ~5000 RPM |
| 80 °C | `disengaged` | ~6300 RPM |
| 90 °C (critical) | `disengaged` + alert | curve is bypassed |

A separate, quieter curve applies on battery. Edit both from the **Fan Curve**
card; changes are picked up without restarting the daemon.

**Hysteresis** (default 5 °C) is what keeps the fan from oscillating. Stepping
*up* happens at the threshold; stepping *down* needs the temperature to fall 5 °C
*below* it. Without it, a temperature sitting on a boundary rewrites the EC
every poll — the old shell daemon did exactly that.

## Safety behaviour

* Every level is validated against a whitelist before reaching `/proc`.
* Every config value the GUI sends is clamped by the root daemon, which is the
  only thing that writes the config file. Invalid fields keep their old value.
* If the temperature sensor becomes unreadable, the daemon hands the fan back
  to the firmware rather than holding a level blind.
* Quitting the app returns the fan to `auto` if the app had taken manual
  control and the daemon isn't running.
* Stopping the daemon (`systemctl stop`) restores `auto` via its SIGTERM handler.
* Optional EC watchdog (`watchdog` in the config, 0–120 s): the embedded
  controller reverts to `auto` if nothing writes a level within the timeout, so
  a hard kill can't leave the fan pinned. Off by default.

## Keyboard shortcuts

`0`–`7` set a fan level · `A` firmware auto · `M` maximum · `D` toggle the daemon.

## Troubleshooting

```bash
systemctl status thinkpad-fan-control    # is the daemon up
tail -f /var/log/thinkpad-fan-control.log
cat /proc/acpi/ibm/fan                   # what the EC currently reports
```

If `/proc/acpi/ibm/fan` says `status: disabled`, fan control isn't enabled in
the driver. `install.sh` writes `options thinkpad_acpi fan_control=1` to
`/etc/modprobe.d/thinkpad_acpi.conf`; that needs a reboot if the module was
already loaded.

Sensors are found by driver name, not by `hwmonN` path — those numbers are
assigned in probe order and do change between boots.
