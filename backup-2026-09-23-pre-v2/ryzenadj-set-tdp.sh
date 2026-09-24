#!/bin/bash
# Secure wrapper for adjusting Ryzen TDP, VRM Current, and Thermal limits
# Installed to /usr/local/bin/ryzenadj-set-tdp.sh

TDP="$1"
VRM_UNLOCKED="${2:-0}"

# Validate that TDP is an integer between 5 and 40 (safe limits for Ryzen laptop APUs)
if ! [[ "$TDP" =~ ^[0-9]+$ ]] || [ "$TDP" -lt 5 ] || [ "$TDP" -gt 40 ]; then
    echo "Invalid TDP: must be an integer between 5 and 40 Watts" >&2
    exit 1
fi

LIMIT=$((TDP * 1000))

if [ "$VRM_UNLOCKED" -eq 1 ]; then
    # Unlocked current limits (EDC=60A, TDC=42A) - "Danger Zone"
    /usr/local/bin/ryzenadj --stapm-limit=$LIMIT --fast-limit=$LIMIT --slow-limit=$LIMIT --vrmmax-current=60000 --vrm-current=42000 --tctl-temp=95
else
    # Default current limits (EDC=45A, TDC=35A)
    /usr/local/bin/ryzenadj --stapm-limit=$LIMIT --fast-limit=$LIMIT --slow-limit=$LIMIT --vrmmax-current=45000 --vrm-current=35000 --tctl-temp=95
fi
