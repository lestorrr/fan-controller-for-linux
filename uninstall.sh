#!/usr/bin/env bash
# =============================================================================
# ThinkPad Fan Control v2 — uninstaller
#
#   sudo ./uninstall.sh [--purge] [--yes] [--user NAME]
#
# Order matters for the fan (docs/CONTRACT.md §18):
#   1. the sudoers rules go first, so a GUI still running in its daemon-less
#      fallback can no longer re-pin the fan through `sudo fan-set-level.sh`
#      (its keep-alive rewrites the level every 30 s);
#   2. the unit is disabled and stopped; the daemon's SIGTERM handler hands
#      the fan back to the firmware (`watchdog 0`, `level auto`);
#   3. the fan is then returned to `auto` once more through the installed
#      wrapper, `fan-set-level.sh auto 0`, BEFORE the wrapper is deleted: a
#      level pinned while the daemon was down (or by a daemon that was killed
#      instead of stopped) must not outlive the last tool that knows about it,
#      and the 0 disarms an EC watchdog left armed by a fallback hold;
#   4. the rest of what install.sh put on the system is removed: the four
#      binaries, the fallback alert sound, the unit, the logrotate snippet and
#      both desktop entries (removed as the desktop user, never by root
#      inside a user-writable directory).
#
# Kept unless --purge: /etc/thinkpad-fan-control (config.json, daemon.env)
# and the log with its rotations. --purge also removes the learned RPM table
# (/var/lib/thinkpad-fan-control), the per-user settings
# (~/.config/thinkpad-fan-control) and /etc/modprobe.d/thinkpad_acpi.conf —
# the latter only when install.sh created it (marker line) and it holds
# nothing but that option, so a fan_control=1 set for another tool survives.
# Backups under /var/backups/thinkpad-fan-control are always kept.
# The checkout itself is never touched.
#
# Safe to re-run: every step skips what is already gone. It takes the same
# lock as install.sh, so the two never run at the same time.
#
# Tests `source` this file: main() only runs when it is executed directly,
# and every system path comes from set_paths() so an offline rehearsal can
# point them at a scratch tree.
# =============================================================================
set -Eeuo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

UNIT_NAME=thinkpad-fan-control.service
# Must match MODPROBE_MARKER in install.sh.
MODPROBE_MARKER="# thinkpad-fan-control: added by install.sh"
BINS=(thinkpad-fan-controld fan-set-level.sh fan-config-save.sh ryzenadj-set-tdp.sh)

# set_paths ROOT — every system path, under an optional prefix used only by
# the offline rehearsal (deliberately not a command-line option).
set_paths() {
    local r=${1:-}
    BIN_DIR=$r/usr/local/bin
    SHARE_DIR=$r/usr/local/share/thinkpad-fan-control
    CONFIG_DIR=$r/etc/thinkpad-fan-control
    UNIT_DST=$r/etc/systemd/system/$UNIT_NAME
    SUDOERS_DST=$r/etc/sudoers.d/fan-control
    LOGROTATE_DST=$r/etc/logrotate.d/thinkpad-fan-control
    MODPROBE_CONF=$r/etc/modprobe.d/thinkpad_acpi.conf
    LOG=$r/var/log/thinkpad-fan-control.log
    STATE_LIB_DIR=$r/var/lib/thinkpad-fan-control
    BACKUP_ROOT=$r/var/backups/thinkpad-fan-control
    RUN_DIR=$r/run/thinkpad-fan-control
    # Shared with install.sh (see its set_paths for why it lives here).
    LOCK_FILE=$r/run/thinkpad-fan-control.install.lock
    FAN_PROC=$r/proc/acpi/ibm/fan
}
set_paths ""

PURGE=0 YES=0 OPT_USER=""
TARGET_USER="" TARGET_HOME=""
PROBLEMS=0 UNIT_KNOWN=0

if [[ -t 1 ]]; then
    RED=$'\033[0;31m' GREEN=$'\033[0;32m' YELLOW=$'\033[1;33m'
    CYAN=$'\033[0;36m' BOLD=$'\033[1m' NC=$'\033[0m'
else
    RED='' GREEN='' YELLOW='' CYAN='' BOLD='' NC=''
fi
say()  { echo "${CYAN}[·]${NC} $*"; }
ok()   { echo "${GREEN}[✓]${NC} $*"; }
warn() { echo "${YELLOW}[!]${NC} $*"; }
err()  { echo "${RED}[✗]${NC} $*" >&2; }
die()  { err "$@"; exit 1; }
step() { printf '\n%s%s%s\n' "$BOLD" "$1" "$NC"; }
# problem MESSAGE — a step that could not finish; the run continues (an
# uninstaller that stops half way leaves more behind than one that goes on)
# and exits 1 at the end.
problem() { warn "$*"; PROBLEMS=$((PROBLEMS + 1)); }

usage() {
    cat <<EOF
Usage: sudo $0 [--purge] [--yes] [--user NAME]

Stops and removes ThinkPad Fan Control: the daemon and its unit, the root
wrappers, the sudoers rules, the logrotate snippet and the desktop entries.
The fan is handed back to the firmware (level auto) before the wrapper that
can do so is removed.

  --purge      also remove /etc/thinkpad-fan-control (config.json,
               daemon.env), the log and its rotations, the learned RPM table,
               the per-user settings and the modprobe.d option install.sh
               added (only if that file holds nothing else)
  --yes        do not ask for confirmation (required without a terminal)
  --user NAME  the desktop user whose entries and settings to remove
               (default: \$SUDO_USER, then logname)
EOF
}

# ── Helpers (pure unless noted) ──────────────────────────────────────────────

# fan_control_active FAN_PROC — thinkpad_acpi was loaded with fan_control=1:
# only then does it print "commands: level <level> ...". "status: disabled"
# is NOT the indicator; it only means the fan sits at level 0.
fan_control_active() {
    grep -qE '^commands:[[:space:]]*level <level>' "$1" 2>/dev/null
}

# fan_proc_level FAN_PROC — the "level:" value, or nothing.
fan_proc_level() {
    sed -n 's/^level:[[:space:]]*//p' "$1" 2>/dev/null | head -n 1 || true
}

# only_our_modprobe_line FILE — FILE was created by install.sh (marker line)
# and holds nothing but "options thinkpad_acpi fan_control=1" besides blank
# lines and comments, so deleting it can take no other option with it.
only_our_modprobe_line() {
    local rest
    [[ -f $1 ]] || return 1
    grep -qxF "$MODPROBE_MARKER" "$1" || return 1
    rest=$(grep -vE '^[[:space:]]*(#|$)' "$1" \
           | grep -vxE '[[:space:]]*options[[:space:]]+thinkpad[-_]acpi[[:space:]]+fan_control=1[[:space:]]*' || true)
    [[ -z $rest ]]
}

# gui_pids USER SRC — pids of Fan Control GUIs (app.py / server.py) of the
# checkout SRC running as USER. A copy of install.sh's function (this script
# must work on its own); the offline tests check that both agree. Only
# Python interpreters count; a relative script argument is resolved against
# the process's cwd; the contract's literal "fan-gui/app.py" also counts.
gui_pids() {
    local user=$1 src=$2 pid arg cwd app_real srv_real
    local -a argv
    local -A found=()
    app_real=$(realpath -m -- "$src/app.py")
    srv_real=$(realpath -m -- "$src/server.py")
    for pid in $(pgrep -u "$user" -f '(app|server)\.py' 2>/dev/null || true); do
        mapfile -d '' -t argv 2>/dev/null < "/proc/$pid/cmdline" || continue
        (( ${#argv[@]} >= 2 )) || continue
        [[ ${argv[0]##*/} == python* ]] || continue
        cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
        for arg in "${argv[@]:1}"; do
            case $arg in app.py|*/app.py|server.py|*/server.py) ;; *) continue ;; esac
            if [[ $arg == *fan-gui/app.py ]]; then found[$pid]=1; break; fi
            if [[ $arg != /* ]]; then
                [[ -n $cwd ]] || continue
                arg=$cwd/$arg
            fi
            arg=$(realpath -m -- "$arg")
            if [[ $arg == "$app_real" || $arg == "$srv_real" ]]; then found[$pid]=1; break; fi
        done
    done
    if (( ${#found[@]} )); then printf '%s\n' "${!found[@]}" | sort -n; fi
    return 0
}

# ── System helpers ───────────────────────────────────────────────────────────

# as_user CMD… — run as the desktop user (files under $HOME are removed this
# way, so a symlink planted there cannot redirect a root unlink).
as_user() {
    runuser -u "$TARGET_USER" -- "$@"
}

# remove_path PATH — rm -f with one line of output; silent when absent.
remove_path() {
    if [[ -e $1 || -L $1 ]]; then
        if rm -f -- "$1"; then ok "removed $1"; else problem "could not remove $1"; fi
    fi
    return 0
}

# remove_user_path PATH — the same, done as the desktop user.
remove_user_path() {
    # shellcheck disable=SC2016  # $1 belongs to the inner sh
    if as_user /bin/sh -c '[ -e "$1" ] || [ -L "$1" ]' fanctl-test "$1" 2>/dev/null; then
        if as_user rm -f -- "$1"; then ok "removed $1"; else problem "could not remove $1"; fi
    fi
    return 0
}

acquire_lock() {
    exec 9>>"$LOCK_FILE" || die "cannot open the lock file $LOCK_FILE"
    flock -n 9 || die "install.sh or uninstall.sh is already running (lock: $LOCK_FILE)"
}

on_err() {
    local rc=$? line=${BASH_LINENO[0]:-?}
    trap - ERR
    err "uninstall.sh stopped at line $line (exit $rc): ${BASH_COMMAND:-?}"
    err "It is safe to run it again; every step skips what is already gone."
    exit 1
}

# ── END SECTION 1 ──
# ── Steps ────────────────────────────────────────────────────────────────────

parse_args() {
    while (( $# )); do
        case $1 in
            --purge) PURGE=1; shift ;;
            --yes|-y) YES=1; shift ;;
            --user)
                if (( $# < 2 )); then usage >&2; exit 2; fi
                OPT_USER=$2; shift 2 ;;
            --user=*) OPT_USER=${1#--user=}; shift ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; err "unknown argument: $1"; exit 2 ;;
        esac
    done
}

# resolve_target_user — lenient on purpose: without a desktop user the
# system part is still removed; only the per-user files are left (and said so).
resolve_target_user() {
    local u=${OPT_USER:-${SUDO_USER:-}} ent home
    if [[ -z $u ]]; then u=$(logname 2>/dev/null || true); fi
    if [[ -z $u || $u == root ]]; then
        warn "could not determine the desktop user; desktop entries and per-user settings stay (re-run with --user NAME)"
        return 0
    fi
    if ! ent=$(getent passwd "$u"); then
        warn "user '$u' not found; desktop entries and per-user settings stay"
        return 0
    fi
    home=$(cut -d: -f6 <<<"$ent")
    if [[ $home != /* || ! -d $home ]]; then
        warn "home directory '$home' of $u does not exist; desktop entries and per-user settings stay"
        return 0
    fi
    TARGET_USER=$u TARGET_HOME=$home
}

confirm() {
    local answer
    echo
    echo "${BOLD}ThinkPad Fan Control — uninstall${NC}"
    echo
    echo "  Stop + disable   $UNIT_NAME (the fan goes back to firmware control)"
    echo "  Remove           ${BINS[*]/#/$BIN_DIR/}"
    echo "                   $SHARE_DIR  $UNIT_DST"
    echo "                   $SUDOERS_DST  $LOGROTATE_DST"
    if [[ -n $TARGET_USER ]]; then
        echo "                   $TARGET_HOME/Desktop/Fan Control.desktop"
        echo "                   $TARGET_HOME/.config/autostart/fan-control.desktop"
    fi
    if (( PURGE )); then
        echo "  --purge, also    $CONFIG_DIR  $LOG*  $STATE_LIB_DIR"
        if [[ -n $TARGET_USER ]]; then echo "                   $TARGET_HOME/.config/thinkpad-fan-control"; fi
        echo "                   $MODPROBE_CONF (only if install.sh created it and it holds nothing else)"
    else
        echo "  Kept             $CONFIG_DIR  $LOG*  $STATE_LIB_DIR  (--purge removes them)"
    fi
    echo "  Always kept      $BACKUP_ROOT  and this checkout ($SRC)"
    echo
    if (( YES )); then return 0; fi
    [[ -t 0 ]] || die "not a terminal; pass --yes to uninstall without confirmation"
    read -r -p "Proceed? [y/N] " answer
    [[ $answer == [yY] ]] || die "aborted; nothing was changed"
}

revoke_sudo() {
    step "1. sudo rules"
    # First, while the daemon still runs: a GUI in daemon-less fallback
    # would otherwise re-write its level through sudo after step 3.
    if [[ -e $SUDOERS_DST || -L $SUDOERS_DST ]]; then
        remove_path "$SUDOERS_DST"
    else
        say "$SUDOERS_DST not present"
    fi
}

stop_daemon() {
    step "2. Daemon"
    local known=0 was_active=0
    if [[ -e $UNIT_DST ]]; then known=1; fi
    if systemctl is-active --quiet "$UNIT_NAME"; then known=1 was_active=1; fi
    if systemctl is-enabled --quiet "$UNIT_NAME" 2>/dev/null; then
        known=1
        if systemctl disable -q "$UNIT_NAME"; then ok "$UNIT_NAME disabled"; else problem "systemctl disable $UNIT_NAME failed"; fi
    fi
    # Stopped whenever the unit exists, not only when it reads "active": a
    # unit in "activating" (a start waiting for the hardware, or an
    # auto-restart pending) must not come up after its files are gone.
    if (( known )); then
        # Synchronous: returns after the daemon wrote 'level auto' and exited
        # (TimeoutStopSec=10, then systemd kills it; step 3 covers that case).
        if systemctl stop "$UNIT_NAME"; then
            if (( was_active )); then
                ok "$UNIT_NAME stopped (its shutdown returned the fan to firmware control)"
            else
                say "$UNIT_NAME was not running"
            fi
        else
            problem "systemctl stop $UNIT_NAME failed"
        fi
    fi
    systemctl reset-failed "$UNIT_NAME" 2>/dev/null || true
    if (( ! known )); then say "$UNIT_NAME is not installed or loaded"; fi
    UNIT_KNOWN=$known
    return 0
}

# return_fan_to_auto — `fan-set-level.sh auto 0` through the installed
# wrapper while it still exists (a v1 wrapper ignores the watchdog argument
# and still writes 'level auto'). Only when the wrapper is already gone is
# the same pair written directly, as the wrapper would.
return_fan_to_auto() {
    step "3. Fan back to firmware control"
    local lvl
    if [[ ! -e $FAN_PROC ]]; then
        say "$FAN_PROC does not exist (thinkpad_acpi not loaded): the firmware drives the fan"
        return 0
    fi
    if ! fan_control_active "$FAN_PROC"; then
        say "thinkpad_acpi offers no fan level control (no 'commands: level' line): nothing can hold the fan, the firmware drives it"
        return 0
    fi
    lvl=$(fan_proc_level "$FAN_PROC")
    if [[ ! -x $BIN_DIR/fan-set-level.sh ]] && (( ! UNIT_KNOWN )); then
        # A re-run after a complete uninstall: nothing of ours is left that
        # could hold the fan, and another fan tool may own it by now.
        say "nothing of this project is installed that could hold the fan (level ${lvl:-unknown}); left alone"
        return 0
    fi
    if [[ -x $BIN_DIR/fan-set-level.sh ]]; then
        if "$BIN_DIR/fan-set-level.sh" auto 0; then
            ok "fan-set-level.sh auto 0: level auto, EC watchdog off (level was ${lvl:-unknown})"
            return 0
        fi
        warn "fan-set-level.sh auto 0 failed; writing to $FAN_PROC directly"
    fi
    if printf 'watchdog 0\n' > "$FAN_PROC" && printf 'level auto\n' > "$FAN_PROC"; then
        ok "wrote 'watchdog 0' and 'level auto' to $FAN_PROC (level was ${lvl:-unknown})"
    else
        problem "could not return the fan to 'auto'. Run: echo 'level auto' | sudo tee $FAN_PROC"
    fi
    return 0
}

remove_files() {
    step "4. Installed files"
    local f
    for f in "${BINS[@]}"; do remove_path "$BIN_DIR/$f"; done
    remove_path "$SHARE_DIR/alert.wav"
    if [[ -d $SHARE_DIR ]] && rmdir -- "$SHARE_DIR" 2>/dev/null; then ok "removed $SHARE_DIR"; fi
    remove_path "$LOGROTATE_DST"
    if [[ -e $UNIT_DST || -L $UNIT_DST ]]; then
        remove_path "$UNIT_DST"
        systemctl daemon-reload || problem "systemctl daemon-reload failed"
        systemctl reset-failed "$UNIT_NAME" 2>/dev/null || true
    fi
    # systemd removes RuntimeDirectory= on stop; this only clears leftovers
    # of a daemon that was killed rather than stopped.
    if [[ -d $RUN_DIR ]]; then
        if rm -rf -- "$RUN_DIR"; then ok "removed $RUN_DIR"; else problem "could not remove $RUN_DIR"; fi
    fi
    if [[ -n $TARGET_USER ]]; then
        remove_user_path "$TARGET_HOME/Desktop/Fan Control.desktop"
        remove_user_path "$TARGET_HOME/.config/autostart/fan-control.desktop"
    fi
    return 0
}

purge_data() {
    step "5. Purge (--purge)"
    local f
    if [[ -d $CONFIG_DIR ]]; then
        if rm -rf -- "$CONFIG_DIR"; then ok "removed $CONFIG_DIR"; else problem "could not remove $CONFIG_DIR"; fi
    fi
    for f in "$LOG" "$LOG".[0-9]*; do remove_path "$f"; done
    if [[ -d $STATE_LIB_DIR ]]; then
        if rm -rf -- "$STATE_LIB_DIR"; then ok "removed $STATE_LIB_DIR"; else problem "could not remove $STATE_LIB_DIR"; fi
    fi
    if [[ -n $TARGET_USER ]]; then
        # shellcheck disable=SC2016
        if as_user /bin/sh -c '[ -d "$1" ]' fanctl-test "$TARGET_HOME/.config/thinkpad-fan-control" 2>/dev/null; then
            if as_user rm -rf -- "$TARGET_HOME/.config/thinkpad-fan-control"; then
                ok "removed $TARGET_HOME/.config/thinkpad-fan-control"
            else
                problem "could not remove $TARGET_HOME/.config/thinkpad-fan-control"
            fi
        fi
    fi
    if only_our_modprobe_line "$MODPROBE_CONF"; then
        remove_path "$MODPROBE_CONF"
        say "after the next boot thinkpad_acpi loads without fan_control=1 (the fan is then read-only)"
    elif [[ -f $MODPROBE_CONF ]]; then
        say "$MODPROBE_CONF kept: not created by install.sh, or it holds other options"
    fi
    return 0
}

report_kept() {
    step "5. Kept (remove with --purge)"
    local any=0
    if [[ -d $CONFIG_DIR ]]; then say "$CONFIG_DIR (config.json, daemon.env)"; any=1; fi
    if [[ -e $LOG ]]; then say "$LOG and its rotations"; any=1; fi
    if [[ -d $STATE_LIB_DIR ]]; then say "$STATE_LIB_DIR (learned RPM per level)"; any=1; fi
    if (( ! any )); then say "nothing"; fi
    return 0
}

notice_running_gui() {
    local pids
    [[ -n $TARGET_USER ]] || return 0
    pids=$(gui_pids "$TARGET_USER" "$SRC" | tr '\n' ' ')
    if [[ -n ${pids// /} ]]; then
        warn "A Fan Control GUI is still running (pid ${pids% }). Quit it: without the daemon and the sudo rules it can only read sensors now."
    fi
    return 0
}

# run_uninstall — everything after the root check. Offline tests call it in a
# subshell with set_paths pointed at a scratch tree and systemctl stubbed.
run_uninstall() {
    set -Eeuo pipefail
    trap on_err ERR
    acquire_lock
    resolve_target_user
    confirm
    revoke_sudo
    stop_daemon
    return_fan_to_auto
    remove_files
    if (( PURGE )); then purge_data; else report_kept; fi
    notice_running_gui
    echo
    if [[ -d $BACKUP_ROOT ]]; then say "backups of earlier installs remain in $BACKUP_ROOT"; fi
    if (( PROBLEMS )); then
        # exit, not return: a failing return would fire the ERR trap's
        # "stopped at line" message although every step ran.
        err "Uninstall finished with $PROBLEMS problem(s) shown above; fix them and run it again."
        exit 1
    fi
    ok "${BOLD}Uninstalled.${NC} The fan is under firmware control."
    echo
}

main() {
    parse_args "$@"
    if (( EUID != 0 )); then
        err "run as root: sudo $0"
        exit 1
    fi
    run_uninstall
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
