#!/usr/bin/env python3
"""
ThinkPad Fan Control — native GTK application.

Embeds the dashboard in a WebKitGTK window and puts a live temperature readout
in the system tray. All hardware access goes through fanlib, which server.py
shares, so there is only one copy of that logic.

    app.py            open the window
    app.py --tray     start hidden in the tray (used by the autostart entry)
"""

import argparse
import os
import signal
import sys
import threading
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Notify", "0.7")
from gi.repository import Gtk, WebKit2, GLib, Gdk, GdkPixbuf, Notify

# The tray icon is optional — the app still works fine without it.
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator
    except (ValueError, ImportError):
        AppIndicator = None

import fanlib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_FILE  = os.path.join(SCRIPT_DIR, "icon.png")
APP_ID     = "com.thinkpad.fancontrol"


class FanControlApp(Gtk.Application):

    def __init__(self, start_hidden=False):
        super().__init__(application_id=APP_ID)
        self.start_hidden = start_hidden
        self.win = None
        self.indicator = None
        self.last_alert = 0.0
        self.status = {}
        self.connect("activate", self.on_activate)

    # ── window ───────────────────────────────────────────────────────────────

    def on_activate(self, _app):
        if self.win:
            self.present_window()
            return

        Notify.init("ThinkPad Fan Control")

        win = Gtk.ApplicationWindow(application=self)
        win.set_title("ThinkPad Fan Control")
        win.set_default_size(1180, 860)
        win.set_position(Gtk.WindowPosition.CENTER)
        if os.path.exists(ICON_FILE):
            win.set_icon_from_file(ICON_FILE)

        # Match the page background so there's no white flash before first paint.
        css = Gtk.CssProvider()
        css.load_from_data(b"window { background-color: #070b14; }")
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        settings = WebKit2.Settings()
        settings.set_enable_javascript(True)
        settings.set_enable_developer_extras(True)     # right-click → Inspect
        settings.set_hardware_acceleration_policy(
            WebKit2.HardwareAccelerationPolicy.ALWAYS)
        settings.set_enable_smooth_scrolling(True)

        self.webview = WebKit2.WebView()
        self.webview.set_settings(settings)
        self.webview.set_background_color(Gdk.RGBA(0.027, 0.043, 0.078, 1.0))
        self.webview.load_uri(f"http://127.0.0.1:{fanlib.PORT}/")

        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title("Fan Control")
        hb.set_subtitle("ThinkPad T495 · thinkpad_acpi")
        if os.path.exists(ICON_FILE):
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(ICON_FILE, 24, 24, True)
            hb.pack_start(Gtk.Image.new_from_pixbuf(pb))

        reload_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic",
                                                   Gtk.IconSize.BUTTON)
        reload_btn.set_tooltip_text("Reload dashboard")
        reload_btn.connect("clicked", lambda *_: self.webview.reload())
        hb.pack_end(reload_btn)

        win.set_titlebar(hb)
        win.add(self.webview)

        # Closing the window leaves the app running in the tray, the same way
        # any other monitor applet behaves. Quit lives in the tray menu.
        win.connect("delete-event", self.on_close_window)
        self.win = win

        self.build_indicator()

        if not self.start_hidden:
            win.show_all()
        else:
            self.hold()      # nothing is visible yet; keep the app alive

        threading.Thread(target=self.poll_loop, daemon=True).start()

    def on_close_window(self, win, _event):
        if self.indicator:
            win.hide()
            return True      # stop the default handler from destroying it
        self.do_quit()
        return True

    def present_window(self, *_):
        self.win.show_all()
        self.win.present()

    # ── tray ─────────────────────────────────────────────────────────────────

    def build_indicator(self):
        if AppIndicator is None:
            return
        icon = ICON_FILE if os.path.exists(ICON_FILE) else "sensors-temperature-symbolic"
        self.indicator = AppIndicator.Indicator.new(
            APP_ID, icon, AppIndicator.IndicatorCategory.HARDWARE)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("ThinkPad Fan Control")

        menu = Gtk.Menu()
        self.mi_status = Gtk.MenuItem(label="Starting…")
        self.mi_status.set_sensitive(False)
        menu.append(self.mi_status)
        menu.append(Gtk.SeparatorMenuItem())

        show = Gtk.MenuItem(label="Open dashboard")
        show.connect("activate", self.present_window)
        menu.append(show)
        menu.append(Gtk.SeparatorMenuItem())

        self.mi_daemon = Gtk.CheckMenuItem(label="Auto curve (daemon)")
        self.daemon_handler = self.mi_daemon.connect("toggled", self.on_daemon_toggled)
        menu.append(self.mi_daemon)

        for label, level in (("Firmware auto", "auto"),
                             ("Quiet · level 2", "2"),
                             ("Balanced · level 4", "4"),
                             ("Maximum · disengaged", "disengaged")):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", self.on_level_clicked, level)
            menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: self.do_quit())
        menu.append(quit_item)

        menu.show_all()
        self.indicator.set_menu(menu)

    def on_level_clicked(self, _item, level):
        def work():
            if fanlib.DAEMON.get():
                fanlib.daemon_ctrl("stop")
                time.sleep(0.3)
            fanlib.set_fan_level(level)
        threading.Thread(target=work, daemon=True).start()

    def on_daemon_toggled(self, item):
        want = item.get_active()
        threading.Thread(
            target=lambda: fanlib.daemon_ctrl("start" if want else "stop"),
            daemon=True).start()

    # ── polling ──────────────────────────────────────────────────────────────

    def poll_loop(self):
        while True:
            try:
                status = fanlib.get_status()
                GLib.idle_add(self.apply_status, status)
            except Exception as e:
                print(f"  [ERR] poll_loop: {e}")
            time.sleep(4)

    def apply_status(self, s):
        self.status = s
        if self.indicator:
            temp = s["temp_c"]
            rpm = max(s["fan1_rpm"], s["fan2_rpm"])
            self.indicator.set_label(f" {temp}°C", "100°C")
            mode = "auto curve" if s["daemon_active"] else f"level {s['level']}"
            self.mi_status.set_label(f"{temp}°C · {rpm} RPM · {mode}")
            # Reflect daemon state without re-triggering our own handler.
            self.mi_daemon.handler_block(self.daemon_handler)
            self.mi_daemon.set_active(s["daemon_active"])
            self.mi_daemon.handler_unblock(self.daemon_handler)

        crit = fanlib.load_config()["critical_temp"]
        now = time.monotonic()
        if s["temp_c"] >= crit and now - self.last_alert > 120:
            self.last_alert = now
            self.notify_critical(s["temp_c"], crit)
        return False       # one-shot idle callback

    def notify_critical(self, temp, crit):
        try:
            n = Notify.Notification.new(
                "ThinkPad overheating",
                f"{temp}°C is at or above the {crit}°C critical threshold. "
                f"Fan forced to maximum.",
                "dialog-warning")
            n.set_urgency(Notify.Urgency.CRITICAL)
            n.show()
        except Exception as e:
            print(f"  [ERR] notify: {e}")

    # ── shutdown ─────────────────────────────────────────────────────────────

    def do_quit(self):
        # Never leave the fan pinned at a level nothing is supervising.
        try:
            fanlib.release_fan()
        except Exception as e:
            print(f"  [ERR] release_fan: {e}")
        try:
            Notify.uninit()
        except Exception:
            pass
        self.quit()


def main():
    ap = argparse.ArgumentParser(description="ThinkPad Fan Control")
    ap.add_argument("--tray", action="store_true",
                    help="start hidden in the system tray")
    ap.add_argument("--port", type=int, default=fanlib.PORT)
    args = ap.parse_args()

    fanlib.PORT = args.port
    try:
        fanlib.start_background(args.port)
    except OSError as e:
        print(f"Could not bind port {args.port}: {e}\n"
              f"Another copy of the app is probably already running.")
        return 1

    time.sleep(0.3)                       # let the socket come up before loading

    app = FanControlApp(start_hidden=args.tray)
    signal.signal(signal.SIGINT, lambda *_: app.do_quit())
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
