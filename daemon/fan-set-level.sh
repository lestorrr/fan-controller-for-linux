#!/bin/bash
# fan-set-level.sh <level> [watchdog] — direct EC write (docs/CONTRACT.md §12).
#
# Runs as root through sudo.  The backend calls it ONLY while the daemon is
# not running: a running daemon owns the fan, re-asserts its own level within
# one sample ("external fan write detected") and takes manual holds through
# its control socket instead.
#
# sudo grants this script with unrestricted arguments, so both arguments are
# matched against exact whitelists and nothing is ever interpolated into a
# command: the only strings written are the literal commands thinkpad_acpi
# accepts.
#
# The optional watchdog is written BEFORE the level, so a manual level set by
# a GUI that later crashes is bounded: thinkpad_acpi hands the fan back to
# firmware auto <watchdog> seconds after the last fan command.  "0" disarms it
# (the backend hands the fan back with "auto 0").
set -euo pipefail

FAN=/proc/acpi/ibm/fan

usage() {
    echo "usage: fan-set-level.sh <auto|0-7|disengaged|full-speed> [watchdog 0-120]" >&2
}

if (( $# < 1 || $# > 2 )); then
    usage
    exit 1
fi

level=$1
case $level in
    auto|disengaged|full-speed|0|1|2|3|4|5|6|7) ;;
    *)
        echo "Invalid level: $level" >&2
        exit 1
        ;;
esac

watchdog=
if (( $# == 2 )); then
    # 0, 1-99, 100-119, 120: the exact range thinkpad_acpi accepts; no
    # leading zeros, signs or whitespace.
    if [[ ! $2 =~ ^(0|[1-9][0-9]?|1[01][0-9]|120)$ ]]; then
        echo "Invalid watchdog: $2 (0 or 1..120 seconds)" >&2
        exit 1
    fi
    watchdog=$2
fi

if [[ ! -e $FAN ]]; then
    echo "$FAN does not exist: is the thinkpad_acpi module loaded?" >&2
    exit 1
fi

# write CMD — one fan command.  Root passes any permission check, so the
# write itself is what tells us whether fan control is possible (without
# fan_control=1 thinkpad_acpi rejects every command); its status is checked
# explicitly and reported with the likely cause.
write() {
    if ! printf '%s\n' "$1" 2>/dev/null > "$FAN"; then
        echo "writing '$1' to $FAN failed: is thinkpad_acpi loaded with fan_control=1?" >&2
        exit 1
    fi
}

if [[ -n $watchdog ]]; then
    write "watchdog $watchdog"
fi
write "level $level"
