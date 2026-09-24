#!/bin/bash
# ryzenadj-set-tdp.sh <watts> <vrm> — root-side TDP / VRM wrapper (docs/CONTRACT.md §12)
#
# Installed to /usr/local/bin/ryzenadj-set-tdp.sh and run through sudo by the
# backend. It is the last line of defence against a GUI bug re-applying a
# dangerous power configuration, so every check here is strict and does not
# rely on anything the caller already validated.
#
#   watts  5..40, no leading zeros ("010" would be octal 8 inside $(( )))
#   vrm    0 = stock VRM current limits  (EDC 45 A / TDC 35 A)
#          1 = unlocked current limits   (EDC 60 A / TDC 42 A)
#
# Caps come from /etc/thinkpad-fan-control/daemon.env (root-owned, written by
# install.sh): FANCTL_TDP_MAX (default 35) and FANCTL_TDP_MAX_VRM_UNLOCKED
# (default 30). A request above them exits 2 with a message on stderr.
#
# Exit codes: 0 applied, 1 bad arguments (or ryzenadj's own failure code),
# 2 refused by a cap.
set -euo pipefail

if (( $# != 2 )); then
    echo "usage: ryzenadj-set-tdp.sh <watts 5-40> <vrm 0|1>" >&2
    exit 1
fi
# Lowercase names: daemon.env only defines FANCTL_* keys; the request is also
# made readonly after validation, so a stray assignment in the sourced file
# fails (and set -e aborts) instead of silently replacing it.
req_watts="$1"
req_vrm="$2"

if [[ ! "$req_watts" =~ ^([5-9]|[1-3][0-9]|40)$ ]]; then
    echo "Invalid TDP '${req_watts}': must be a whole number of watts from 5 to 40 (no leading zeros)" >&2
    exit 1
fi
if [[ ! "$req_vrm" =~ ^[01]$ ]]; then
    echo "Invalid VRM flag '${req_vrm}': must be 0 (stock) or 1 (unlocked)" >&2
    exit 1
fi

# Validated: nothing below (in particular the sourced env file) may change them.
readonly req_watts req_vrm

# The env file is optional (a first run before install.sh wrote it); the caps
# then fall back to the defaults. A malformed cap must not disable the guard,
# so anything but a plain 1-2 digit integer is replaced by its default. Caps
# inherited from the caller's environment never count: only the root-owned
# file or the defaults (sudo resets the environment anyway; root run by hand
# should get the same answer).
unset FANCTL_TDP_MAX FANCTL_TDP_MAX_VRM_UNLOCKED
env_file=/etc/thinkpad-fan-control/daemon.env
if [[ -r "$env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$env_file"
    set +a
fi
cap_max="${FANCTL_TDP_MAX:-35}"
cap_max_unlocked="${FANCTL_TDP_MAX_VRM_UNLOCKED:-30}"
[[ "$cap_max" =~ ^[1-9][0-9]?$ ]] || cap_max=35
[[ "$cap_max_unlocked" =~ ^[1-9][0-9]?$ ]] || cap_max_unlocked=30

if (( req_watts > cap_max )); then
    echo "Refused: ${req_watts} W exceeds FANCTL_TDP_MAX=${cap_max} W" >&2
    exit 2
fi
if [[ "$req_vrm" == "1" ]] && (( req_watts > cap_max_unlocked )); then
    echo "Refused: ${req_watts} W with the VRM current limits unlocked exceeds FANCTL_TDP_MAX_VRM_UNLOCKED=${cap_max_unlocked} W" >&2
    exit 2
fi

limit_mw=$(( req_watts * 1000 ))
if [[ "$req_vrm" == "1" ]]; then
    vrm_max_ma=60000      # EDC 60 A
    vrm_cur_ma=42000      # TDC 42 A
else
    vrm_max_ma=45000      # EDC 45 A (stock)
    vrm_cur_ma=35000      # TDC 35 A (stock)
fi

# ryzenadj's stdout ("Sucessfully set ...") is noise for the caller; its
# stderr is kept because the backend shows the last stderr line on failure,
# and set -e passes ryzenadj's own exit code through.
/usr/local/bin/ryzenadj \
    --stapm-limit="$limit_mw" --fast-limit="$limit_mw" --slow-limit="$limit_mw" \
    --vrmmax-current="$vrm_max_ma" --vrm-current="$vrm_cur_ma" \
    --tctl-temp=95 >/dev/null

echo "applied ${req_watts}W vrm=${req_vrm}"
