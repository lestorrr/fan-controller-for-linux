#!/usr/bin/env python3
"""
ThinkPad Fan Control — browser mode (docs/CONTRACT.md §16 "server.py").

Serves the same dashboard and API as the GTK app on http://127.0.0.1:7070,
for use from a normal browser or when GTK/WebKit is unavailable. All logic
lives in fanlib; this file only binds, serves and runs the shared release
path on the way out.

    server.py                 serve and open the browser
    server.py --no-browser    serve only
    server.py --port 7150     another port (tests: 7100-7199 with FANCTL_DRY_RUN=1)
"""

import argparse
import errno
import signal
import sys
import threading
import webbrowser

import fanlib


class _Terminate(Exception):
    """Raised by the SIGTERM/SIGHUP handlers so serve_forever() unwinds into the release path."""


def _on_signal(signum, _frame):
    raise _Terminate(signum)


def main():
    ap = argparse.ArgumentParser(description="ThinkPad Fan Control (browser mode)")
    ap.add_argument("--port", type=int, default=fanlib.PORT)
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser window on start")
    args = ap.parse_args()
    fanlib.PORT = args.port

    # Bind before anything else: when another copy owns the port there is
    # nothing to start, and no browser tab should open against that copy.
    try:
        srv = fanlib.make_server(args.port)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print("Port in use — another copy is already running", file=sys.stderr)
        else:
            # Not "in use": an invalid or privileged port would send the user
            # looking for a second copy that does not exist.
            print(f"Cannot listen on 127.0.0.1:{args.port}: {e.strerror or e}", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{args.port}/"
    print("\n  ThinkPad Fan Control (browser mode)")
    print(f"  {url}")
    print("  Ctrl+C to stop\n", flush=True)

    fanlib.set_visibility(True)          # a browser tab counts as a visible window for the SMU cadence
    fanlib.start_workers()

    if not args.no_browser:
        t = threading.Timer(1.0, webbrowser.open, args=(url,))
        t.daemon = True                  # never keep the process alive for a browser
        t.start()

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _on_signal)

    code = 0
    try:
        srv.serve_forever()
    except (KeyboardInterrupt, _Terminate):
        pass
    except Exception as e:               # noqa: BLE001 — the release path must still run
        print(f"  server error: {e!r}", file=sys.stderr)
        code = 1
    finally:
        # A second Ctrl+C / SIGTERM must not interrupt the release half way
        # (it would leave a fallback hold or an unlocked VRM behind).
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        print("\n  Stopping — releasing the fan and power limits.", flush=True)
        fanlib.release_all()             # the same path as app.py's do_quit
        srv.server_close()
    return code


if __name__ == "__main__":
    sys.exit(main())
