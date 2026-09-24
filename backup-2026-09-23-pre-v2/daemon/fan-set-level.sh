#!/bin/bash
# Secure wrapper — only allows valid thinkpad_acpi fan level values.
level="$1"
case "$level" in
    auto|disengaged|full-speed|0|1|2|3|4|5|6|7)
        echo "level $level" > /proc/acpi/ibm/fan && exit 0
        ;;
    *)
        echo "Invalid level: $level" >&2
        exit 1
        ;;
esac
