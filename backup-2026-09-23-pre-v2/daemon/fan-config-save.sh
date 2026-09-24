#!/bin/bash
# Root-side entry point for saving the fan curve config.
# Reads JSON on stdin; the daemon re-validates and clamps every field before
# writing it, so nothing here needs to trust the caller.
exec /usr/local/bin/thinkpad-fan-controld --save-config
