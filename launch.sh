#!/usr/bin/env bash
# =============================================================================
# ThinkPad Fan Control — GUI launcher
#
#   ./launch.sh            dashboard window
#   ./launch.sh --tray     start hidden in the tray (what the autostart entry uses)
#
# Runs `python3 app.py "$@"` from the checkout (docs/CONTRACT.md §18).
# readlink -f follows a symlinked launcher back to the real checkout; the cd
# keeps relative paths inside app.py/fanlib.py working. app.py is passed by
# its absolute path so the process's command line names the checkout: that is
# what install.sh / uninstall.sh look for when they tell you to relaunch a
# running copy. Arguments pass straight through. Browser-only mode is
# `python3 server.py` — see README.md.
# =============================================================================
set -euo pipefail
DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
cd "$DIR"
exec python3 "$DIR/app.py" "$@"
