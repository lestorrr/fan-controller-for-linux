#!/usr/bin/env python3
"""
ThinkPad Fan Control — native GTK application (docs/CONTRACT.md §16).

Embeds the dashboard in a WebKitGTK window and puts a live temperature
readout in the system tray. All hardware access goes through fanlib, which
server.py shares, so there is exactly one copy of that logic.

    app.py            open the window
    app.py --tray     start hidden in the tray (the autostart entry uses this)
    app.py --port N   serve the API on another port

Why it is built this way:
  * Gtk.Application is constructed first and the HTTP port is bound in the
    `startup` handler. Only the primary instance receives `startup`, so a
    second launch never fights for the port: its `activate` is forwarded to
    the running copy, which raises its window.
  * The WebView is created on first present and shows about:blank while the
    window is hidden, so a tray-only session runs no page and no page polling.
  * One poller thread feeds the tray and the notifications; the page polls
    the API on its own and never talks to the tray.
  * Quitting (menu, window close without a tray, SIGTERM/SIGINT/SIGHUP) runs
    fanlib.release_all() on a worker thread, because it may wait on sudo
    for tens of seconds and the GTK main loop must keep running meanwhile.
"""

import argparse
import errno
import os
import signal
import sys
import threading
import time

import gi
# Gdk must be pinned too, and Gtk imported before it: GTK 4 is installed on
# this machine as well, and an unpinned `from gi.repository import Gdk` that
# runs first loads Gdk 4.0, after which Gtk 3.0 refuses to load ("Requiring
# namespace 'Gdk' version '3.0', but '4.0' is already loaded").
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk  # noqa: E402  (first: pulls in Gdk 3.0)
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, WebKit2  # noqa: E402

# libnotify is optional: without it notifications are printed, and a missing
# typelib must not take the tray down with it.
try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify
except (ValueError, ImportError):
    Notify = None

# The tray icon is optional: without it --tray is ignored and the window shows.
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator
    except (ValueError, ImportError):
        AppIndicator = None

import fanlib  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_FILE  = os.path.join(SCRIPT_DIR, "icon.png")
APP_ID     = "com.thinkpad.fancontrol"
POLL_S     = 3
PAGE_BG    = "#070b14"
# Upper bound for the release path before the process quits regardless: a
# stuck sudo must not keep a logging-out session waiting forever.
QUIT_DEADLINE_S = 60

# Tray "Hold fan at…" submenu, in menu order.
HOLD_ITEMS = [(str(i), f"Level {i}") for i in range(8)] + [
    ("auto", "Firmware auto"),
    ("disengaged", "Maximum"),
]

_MODE_WORDS = {
    "curve": "Curve",
    "hold_suspended": "Hold suspended (hot)",
    "critical": "CRITICAL — fan at maximum",
    "firmware": "Firmware auto (no daemon)",
    "sensor_lost": "Sensor lost — firmware auto",
}


def _mmss(seconds):
    s = max(0, int(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def status_line(s):
    """Tray status line: '78 °C · 4000 RPM · Curve' or 'Hold 3 · 12:34 left'."""
    temp, rpm, m = s.get("temp_c"), s.get("fan_rpm"), s.get("mode")
    head = f"{round(temp)} °C" if isinstance(temp, (int, float)) else "-- °C"
    if isinstance(rpm, int):
        head += f" · {rpm} RPM"
    ov = (s.get("state") or {}).get("override") or {}
    if m == "hold":
        rem = ov.get("remaining_s")
        left = f" · {_mmss(rem)} left" if isinstance(rem, (int, float)) else ""
        return f"Hold {ov.get('level', '?')}{left}"
    if m == "manual_unprotected":
        if s.get("manual_fallback"):
            if s.get("manual_critical"):
                return f"{head} · No daemon — critical, fan forced to maximum"
            rem = s.get("manual_fallback_remaining_s")
            left = f" · {_mmss(rem)} left" if isinstance(rem, (int, float)) else ""
            return f"Hold {s.get('manual_fallback_level', '?')} (no daemon){left}"
        return f"{head} · Level {s.get('level')} — no daemon, no protection"
    return f"{head} · {_MODE_WORDS.get(m, m or '?')}"


class FanControlApp(Gtk.Application):

    def __init__(self, port, start_hidden):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.port = port
        self.start_hidden = start_hidden
        self.exit_code = 0
        self.win = None
        self.container = None
        self.webview = None
        self.indicator = None
        self.hold_items = {}
        self._bind_failed = False
        self._activated_once = False
        self._quitting = False
        self._hold_label_secs = None
        self.status = {}
        self.prev_status = None
        self.last_fallback_end = None
        self.connect("startup", self.on_startup)
        self.connect("activate", self.on_activate)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def on_startup(self, _app):
        # Only the primary instance gets here, so only it binds the port.
        try:
            fanlib.start_background(self.port)
        except OSError as e:
            self._bind_failed = True
            self.exit_code = 1
            if e.errno == errno.EADDRINUSE:
                detail = (f"Port {self.port} is already in use.\n"
                          "Another copy (probably server.py) owns it; close that one first.")
            else:
                detail = f"Cannot listen on 127.0.0.1:{self.port}: {e.strerror or e}."
            print(f"Fan Control could not start: {detail}", file=sys.stderr, flush=True)
            dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR,
                                    buttons=Gtk.ButtonsType.CLOSE,
                                    text="Fan Control could not start")
            dlg.format_secondary_text(detail)
            dlg.run()
            dlg.destroy()
            return

        if Notify is not None:
            try:
                Notify.init("ThinkPad Fan Control")
            except Exception as e:             # noqa: BLE001 — no notification daemon is not fatal
                print(f"  [ERR] Notify.init: {e!r}", flush=True)

        if AppIndicator is None and self.start_hidden:
            print("No AppIndicator library available: ignoring --tray and showing the window.", flush=True)
            self.start_hidden = False
        self.build_indicator()
        if self.indicator is not None:
            # With a tray the app outlives its window (closing only hides it).
            self.hold()
        fanlib.set_visibility(not self.start_hidden)

        # Installed here, inside app.run(): PyGObject's own SIGINT fallback
        # (which would call quit() and skip the release path) is set up just
        # before startup, and GLib's handler must be installed after it.
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal, sig)

        threading.Thread(target=self.poll_loop, name="tray-poller", daemon=True).start()

    def on_activate(self, _app):
        if self._bind_failed:
            self.quit()
            return
        first = not self._activated_once
        self._activated_once = True
        if first and self.start_hidden and self.indicator is not None:
            return                 # tray-only start; the hold() from startup keeps us alive
        self.present_window()

    def _on_signal(self, sig):
        print(f"signal {sig}: quitting", flush=True)
        self.do_quit()
        # Keep the source: removing it would restore SIG_DFL, and a second
        # signal during the release would then kill us half way through.
        return GLib.SOURCE_CONTINUE

    def do_quit(self, *_):
        """§16 release path, then quit. Idempotent; never blocks the main loop."""
        if self._quitting:
            return
        self._quitting = True
        if self.win is not None:
            self.win.hide()
        GLib.timeout_add_seconds(QUIT_DEADLINE_S, self._finish_quit, True)

        def work():
            try:
                # Resume an override we created, end a daemon-less hold,
                # restore stock VRM if this session unlocked it, clean exit.
                fanlib.release_all()
            except Exception as e:             # noqa: BLE001
                print(f"  [ERR] release_all: {e!r}", flush=True)
            GLib.idle_add(self._finish_quit, False)

        threading.Thread(target=work, name="release", daemon=True).start()

    def _finish_quit(self, timed_out):
        if timed_out:
            print(f"release path still running after {QUIT_DEADLINE_S} s; quitting anyway", flush=True)
        if Notify is not None:
            try:
                Notify.uninit()
            except Exception:                  # noqa: BLE001
                pass
        self.quit()
        return GLib.SOURCE_REMOVE

    # ── window ───────────────────────────────────────────────────────────────

    def build_window(self):
        win = Gtk.ApplicationWindow(application=self)
        win.set_title("ThinkPad Fan Control")
        win.set_default_size(1180, 860)
        win.set_position(Gtk.WindowPosition.CENTER)
        if os.path.exists(ICON_FILE):
            try:
                win.set_icon_from_file(ICON_FILE)
            except GLib.Error:
                pass

        # Match the page background so there is no white flash before first paint.
        css = Gtk.CssProvider()
        css.load_from_data(f"window {{ background-color: {PAGE_BG}; }}".encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title("Fan Control")
        hb.set_subtitle("ThinkPad T495 · thinkpad_acpi")
        if os.path.exists(ICON_FILE):
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(ICON_FILE, 24, 24, True)
                hb.pack_start(Gtk.Image.new_from_pixbuf(pb))
            except GLib.Error:
                pass
        reload_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        reload_btn.set_tooltip_text("Reload dashboard")
        reload_btn.connect("clicked", self.on_reload_clicked)
        hb.pack_end(reload_btn)
        win.set_titlebar(hb)

        self.container = Gtk.Box()
        win.add(self.container)
        # With a tray, closing only hides the window (like any monitor applet).
        win.connect("delete-event", self.on_close_window)
        win.connect("window-state-event", self.on_window_state)
        self.win = win

    def ensure_webview(self):
        if self.webview is not None:
            return
        settings = WebKit2.Settings()
        settings.set_enable_javascript(True)
        settings.set_enable_developer_extras(True)          # right-click → Inspect
        # ON_DEMAND, not ALWAYS: no permanent GPU client for one canvas on
        # the amdgpu that hung on 2026-09-15.
        settings.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.ON_DEMAND)
        settings.set_enable_smooth_scrolling(True)
        wv = WebKit2.WebView()
        wv.set_settings(settings)
        rgba = Gdk.RGBA()
        rgba.parse(PAGE_BG)
        wv.set_background_color(rgba)
        wv.load_uri("about:blank")
        self.container.pack_start(wv, True, True, 0)
        self.webview = wv

    def dashboard_uri(self):
        return f"http://127.0.0.1:{self.port}/?embedded=1"

    def present_window(self, *_):
        if self._quitting:
            return
        if self.win is None:
            self.build_window()
        self.ensure_webview()
        if self.webview.get_uri() != self.dashboard_uri():
            self.webview.load_uri(self.dashboard_uri())
        self.win.show_all()
        self.win.present()
        fanlib.set_visibility(True)

    def hide_window(self):
        self.win.hide()
        if self.webview is not None:
            self.webview.load_uri("about:blank")        # no page, no page polling while hidden
        fanlib.set_visibility(False)

    def on_close_window(self, _win, _event):
        if self.indicator is not None:
            self.hide_window()
        else:
            self.do_quit()          # no tray: closing the window is the only way out
        return True                 # never let GTK destroy the window itself

    def on_window_state(self, _win, event):
        if event.changed_mask & Gdk.WindowState.ICONIFIED:
            # Minimised counts as hidden for the SMU cadence; the page stays loaded.
            fanlib.set_visibility(not (event.new_window_state & Gdk.WindowState.ICONIFIED))
        return False

    def on_reload_clicked(self, *_):
        if self.webview is not None:
            self.webview.load_uri(self.dashboard_uri())

    # ── tray ─────────────────────────────────────────────────────────────────

    def build_indicator(self):
        if AppIndicator is None:
            return
        try:
            icon = ICON_FILE if os.path.exists(ICON_FILE) else "sensors-temperature-symbolic"
            ind = AppIndicator.Indicator.new(APP_ID, icon, AppIndicator.IndicatorCategory.HARDWARE)
        except Exception as e:                 # noqa: BLE001 — degrade to window-only
            print(f"  [ERR] AppIndicator: {e!r}; showing the window instead", flush=True)
            self.start_hidden = False
            return
        ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        ind.set_title("ThinkPad Fan Control")

        menu = Gtk.Menu()
        self.mi_status = Gtk.MenuItem(label="Starting…")
        self.mi_status.set_sensitive(False)
        menu.append(self.mi_status)
        menu.append(Gtk.SeparatorMenuItem())

        mi_open = Gtk.MenuItem(label="Open dashboard")
        mi_open.connect("activate", self.present_window)
        menu.append(mi_open)

        self.mi_hold = Gtk.MenuItem(label="Hold fan at…")
        sub = Gtk.Menu()
        for level, label in HOLD_ITEMS:
            item = Gtk.MenuItem(label=label)
            item.connect("activate", self.on_hold_clicked, level)
            sub.append(item)
            self.hold_items[level] = (item, label)
        self.mi_hold.set_submenu(sub)
        menu.append(self.mi_hold)

        mi_resume = Gtk.MenuItem(label="Resume auto curve")
        mi_resume.connect("activate", self.on_resume_clicked)
        menu.append(mi_resume)

        self.mi_restore = Gtk.MenuItem(label="Restore stock power limits")
        self.mi_restore.connect("activate", self.on_restore_stock_clicked)
        self.mi_restore.set_no_show_all(True)      # shown only while the VRM is unlocked
        menu.append(self.mi_restore)

        menu.append(Gtk.SeparatorMenuItem())
        mi_quit = Gtk.MenuItem(label="Quit")
        mi_quit.connect("activate", self.do_quit)
        menu.append(mi_quit)

        menu.show_all()
        ind.set_menu(menu)
        self.indicator = ind
        self._update_hold_labels(fanlib.get_settings()["ui"]["hold_default_seconds"])

    def _update_hold_labels(self, secs):
        if secs == self._hold_label_secs:
            return
        self._hold_label_secs = secs
        dur = fanlib.fmt_duration(secs)
        self.mi_hold.set_label(f"Hold fan at… ({dur})")
        for item, label in self.hold_items.values():
            item.set_label(f"{label} · {dur}")

    def _run_action(self, fn, what):
        """Run a fanlib action off the main loop; failures become a notification."""
        def work():
            try:
                r = fn()
            except Exception as e:             # noqa: BLE001
                r = {"success": False, "error": repr(e)}
            if not r.get("success"):
                GLib.idle_add(self.notify, f"{what} failed", r.get("error") or "Unknown error",
                              "normal", "dialog-error")
        threading.Thread(target=work, name="tray-action", daemon=True).start()

    def on_hold_clicked(self, _item, level):
        secs = self.status.get("hold_default_seconds")
        if not isinstance(secs, int):
            secs = fanlib.get_settings()["ui"]["hold_default_seconds"]
        self._run_action(lambda: fanlib.fan_hold(level, secs), f"Hold {level}")

    def on_resume_clicked(self, *_):
        self._run_action(fanlib.fan_resume, "Resume")

    def on_restore_stock_clicked(self, *_):
        self._run_action(fanlib.restore_stock, "Restore stock power limits")

    # ── polling ──────────────────────────────────────────────────────────────

    def poll_loop(self):
        while not self._quitting:
            try:
                status = fanlib.get_status()
                fb_end = fanlib.FALLBACK.last_end
                GLib.idle_add(self.apply_status, status, fb_end)
            except Exception as e:             # noqa: BLE001 — the tray must keep updating
                print(f"  [ERR] poll_loop: {e!r}", flush=True)
            time.sleep(POLL_S)

    def apply_status(self, s, fb_end):
        if self._quitting:
            return False
        prev, self.prev_status, self.status = self.prev_status, s, s
        if self.indicator is not None:
            temp = s.get("temp_c")
            self.indicator.set_label(f" {round(temp)}°" if isinstance(temp, (int, float)) else " --°", " 100°")
            text = status_line(s)
            self.mi_status.set_label(text)
            self.indicator.set_title(f"Fan Control — {text}")
            self._update_hold_labels(s.get("hold_default_seconds"))
            self.mi_restore.set_visible(bool(s.get("vrm_unlocked")))
            # Mirrors the daemon's rule (§7): no fan-off at or above 55 °C.
            item0 = self.hold_items.get("0")
            if item0:
                item0[0].set_sensitive(not (isinstance(temp, (int, float)) and temp >= fanlib.FAN_OFF_MAX_TEMP))
        self._notify_edges(prev, s, fb_end)
        return False               # one-shot idle callback

    def _notify_edges(self, prev, s, fb_end):
        # Edge-triggered on the daemon's judgement (state.critical.active),
        # never on our own reading of the temperature (§8).
        st, pst = s.get("state") or {}, (prev or {}).get("state") or {}
        crit_now = bool((st.get("critical") or {}).get("active"))
        crit_before = bool((pst.get("critical") or {}).get("active"))
        if crit_now and not crit_before:
            temp = s.get("temp_c")
            self.notify("ThinkPad overheating",
                        f"{round(temp) if isinstance(temp, (int, float)) else '?'} °C reached the "
                        f"{s.get('critical_temp')} °C critical threshold. The daemon forced the fan to maximum.",
                        "critical", "dialog-warning")
        # The daemon-less keep-alive forcing the fan is an actuation, not a
        # reading, so it gets the same urgent notification.
        if s.get("manual_critical") and not (prev or {}).get("manual_critical"):
            self.notify("ThinkPad overheating — no daemon",
                        f"The fan control daemon is not running. The fan was forced to maximum at "
                        f"{s.get('critical_temp')} °C by the dashboard's fallback.",
                        "critical", "dialog-warning")

        # A daemon hold that ended without anyone in this process asking.
        pov = pst.get("override")
        if (prev and prev.get("daemon_active") and s.get("daemon_active") and isinstance(pov, dict)
                and not st.get("override") and fanlib.last_resume_age() > 2 * POLL_S + 2):
            tail = ("critical protection has the fan at maximum." if crit_now
                    else "the fan is back on the automatic curve.")
            self.notify("Fan hold ended", f"The hold at level {pov.get('level', '?')} ended; {tail}",
                        "normal", "dialog-information")

        # A daemon-less hold that ended by itself (expiry, critical, sensor loss, daemon back).
        if fb_end is not None and fb_end is not self.last_fallback_end:
            first = self.last_fallback_end is None and prev is None
            self.last_fallback_end = fb_end
            if not first:
                why = {"expired": "it ran out",
                       "critical_cleared": "the die cooled down after a critical episode",
                       "sensor_lost": "no temperature sensor could be read",
                       "daemon": "the fan control daemon is running again",
                       "error": "its supervisor failed"}.get(fb_end.get("reason"), "it ended")
                tail = ("the daemon owns the fan again." if fb_end.get("reason") == "daemon"
                        else "the fan is back on firmware auto.")
                self.notify("Fan hold ended", f"The hold at level {fb_end.get('level', '?')} ended because "
                            f"{why}; {tail}", "normal", "dialog-information")

    def notify(self, title, body, urgency, icon):
        if Notify is None:
            print(f"  [notify/{urgency}] {title}: {body}", flush=True)
            return False
        try:
            n = Notify.Notification.new(title, body, icon)
            n.set_urgency(Notify.Urgency.CRITICAL if urgency == "critical" else Notify.Urgency.NORMAL)
            n.show()
        except Exception as e:                 # noqa: BLE001
            print(f"  [ERR] notify: {e!r}", flush=True)
        return False


def main():
    ap = argparse.ArgumentParser(description="ThinkPad Fan Control")
    ap.add_argument("--tray", action="store_true", help="start hidden in the system tray")
    ap.add_argument("--port", type=int, default=fanlib.PORT)
    args = ap.parse_args()
    fanlib.PORT = args.port

    app = FanControlApp(port=args.port, start_hidden=args.tray)
    rc = app.run([sys.argv[0]])        # our own flags were parsed above
    return app.exit_code or rc


if __name__ == "__main__":
    sys.exit(main())
