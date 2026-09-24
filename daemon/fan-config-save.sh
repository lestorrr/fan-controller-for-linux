#!/bin/bash
# fan-config-save.sh — root-side entry point for saving the fan config
# (docs/CONTRACT.md §1, §3, §12).
#
# Reads JSON on stdin and hands it to the daemon binary's --save-config, the
# ONLY code allowed to write /etc/thinkpad-fan-control/config.json.
# Validation happens there, in the same root-owned code that consumes the
# file, so nothing here needs to trust the caller.  The reply (exactly one
# JSON line: {"config": ..., "rejected": [...]}) goes back on stdout.
#
# No arguments are accepted: the sudoers rule grants this script without an
# argument pattern, so refusing argv keeps the grant equivalent to an exact
# one.  The FANCTL_* path overrides cannot redirect the write either: sudo's
# env_reset drops them, and the daemon ignores them whenever it runs as root.
set -euo pipefail

if (( $# != 0 )); then
    echo "usage: fan-config-save.sh < config.json" >&2
    exit 1
fi

exec /usr/local/bin/thinkpad-fan-controld --save-config
