#!/usr/bin/env python3
"""
ThinkPad Fan Control — browser mode.

Serves the same dashboard and API as the GTK app on http://127.0.0.1:7070.
Useful for reaching the UI from another window or when GTK/WebKit isn't
available. All the actual logic lives in fanlib.
"""

import argparse
import threading
import webbrowser

import fanlib


def main():
    ap = argparse.ArgumentParser(description="ThinkPad Fan Control (browser mode)")
    ap.add_argument("--port", type=int, default=fanlib.PORT)
    ap.add_argument("--no-browser", action="store_true",
                    help="don't open a browser window on start")
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    print(f"\n  💨  ThinkPad Fan Control")
    print(f"  ─────────────────────────")
    print(f"  → {url}")
    print(f"  Ctrl+C to stop\n")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    threading.Thread(target=fanlib.tdp_lock_loop, daemon=True).start()
    srv = fanlib.make_server(args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopping — returning fan to firmware control.")
        fanlib.release_fan()


if __name__ == "__main__":
    main()
