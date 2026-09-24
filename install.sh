#!/usr/bin/env bash
# =============================================================================
# ThinkPad Fan Control v2 — installer and upgrader
#
#   sudo ./install.sh [--user NAME]
#
# One command for every case (docs/CONTRACT.md §18): a first install, the
# upgrade from v1, and a re-run over v2. A re-run with nothing changed leaves
# the running daemon alone, so it never drops a manual fan hold for nothing.
#
# Order of operations:
#    0. preflight  byte-compile the Python, bash -n the root wrappers, check
#                  the daemon is a v2 build, systemd-analyze verify the unit,
#                  visudo -c and allow-list the rendered sudoers, check that
#                  alert.wav is a WAV file, and read (only read) whether
#                  thinkpad_acpi was loaded with fan_control=1. The checked
#                  files are copied to a private staging dir first, so what
#                  was verified is byte-for-byte what gets installed.
#                  Nothing on the system changes before this passes.
#    1. backups    every replaced file is copied to
#                  /var/backups/thinkpad-fan-control/<stamp>/<original path>;
#                  "options thinkpad_acpi fan_control=1" is ensured in
#                  /etc/modprobe.d (kept from v1)
#    2. binaries   same names as v1 (a still-running old GUI keeps working)
#                  plus the fallback alert.wav
#    3. daemon.env GUI uid/gid/home/runtime dir + TDP ceilings (§2); values
#                  already in the file are preserved
#    4. config     the NEW daemon normalises config.json via --save-config
#    5. log        one-time rotation of an oversized log + logrotate snippet
#    6. sudoers    template with the user substituted, visudo-checked, atomic
#    7. unit       install, daemon-reload, enable; start if stopped; restart
#                  only when the daemon binary, the unit or daemon.env
#                  changed (EnvironmentFile= is only read at start)
#    8. post-check unit active and a fresh state.json within 10 s, else the
#                  journal is shown, the run is rolled back, the previous
#                  daemon restarted, exit 1
#    9. desktop    launcher + autostart entry, read and written as the
#                  desktop user (root never opens a path under $HOME); the
#                  user's X-GNOME-Autostart-enabled / Hidden choices are
#                  preserved
#   10. notice     tell the user to relaunch a running GUI
#
# When thinkpad_acpi is loaded WITHOUT fan_control=1 (/proc/acpi/ibm/fan has
# no "commands: level ..." line) the self-test write of the daemon could not
# succeed, so everything is installed and enabled but not started, and the
# run ends with REBOOT REQUIRED. "status: disabled" in that file is NOT that
# condition: it only means the fan currently sits at level 0.
#
# Rollback scope. The contract's minimum for a failed post-check is "restore
# the daemon + unit and restart". This script restores EVERY file the run
# replaced (and removes those it created) on any failure in steps 1-8, then
# puts the unit back into the enabled/active state it had before: a v1 daemon
# restarted on top of a schema-2 config, v2 wrappers and a v2 sudoers file is
# a combination nobody tested. Only the one-time log rotation is not undone;
# it loses no line.
#
# The installer never writes to /proc/acpi/ibm/fan. Only the daemon does,
# under systemd, after its own self-test.
#
# Tests `source` this file: main() only runs when it is executed directly,
# and every system path comes from set_paths() so an offline rehearsal can
# point them at a scratch tree.
# =============================================================================
set -Eeuo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Constants ────────────────────────────────────────────────────────────────
UNIT_NAME=thinkpad-fan-control.service
# ExecStart= of the unit. Not prefixed by set_paths: it is what the unit text
# says, and systemd-analyze checks exactly that path.
UNIT_EXEC=/usr/local/bin/thinkpad-fan-controld
# The user name literally written in daemon/fan-control.sudoers.
TEMPLATE_USER=jhnlstrlclcn
# Written above a modprobe option this installer adds, so uninstall.sh
# --purge can tell our file from one written for another tool.
MODPROBE_MARKER="# thinkpad-fan-control: added by install.sh"
LOG_ROTATE_BYTES=$((2 * 1024 * 1024))
POSTCHECK_SECONDS=10
SAVE_CONFIG_TIMEOUT=30
# Ownership of everything installed outside $HOME. Offline tests, which run
# without root, empty these; production never changes them.
OWN_ARGS=(-o root -g root)
OWN_SPEC=root:root

# Installed 0755 into /usr/local/bin; the first three come from daemon/, the
# TDP wrapper from the repo root (backend-owned).
DAEMON_BINS=(thinkpad-fan-controld fan-set-level.sh fan-config-save.sh ryzenadj-set-tdp.sh)
# daemon.env keys in the order they are written (§2) and their defaults.
ENV_KEYS=(FANCTL_GUI_UID FANCTL_GUI_GID FANCTL_GUI_HOME
          FANCTL_GUI_RUNTIME_DIR FANCTL_TDP_MAX FANCTL_TDP_MAX_VRM_UNLOCKED)
ENV_DEFAULT_TDP_MAX=35
ENV_DEFAULT_TDP_MAX_VRM_UNLOCKED=30

# set_paths ROOT — every system path the installer reads or writes, under an
# optional prefix. Production uses "" (set right below). The prefix exists for
# the offline rehearsal only, which is why it is not a command-line option:
# an installer that can be told to write elsewhere invites half-installs.
set_paths() {
    local r=${1:-}
    BIN_DIR=$r/usr/local/bin
    DAEMON=$BIN_DIR/thinkpad-fan-controld
    SHARE_DIR=$r/usr/local/share/thinkpad-fan-control
    CONFIG_DIR=$r/etc/thinkpad-fan-control
    CONFIG=$CONFIG_DIR/config.json
    ENV_FILE=$CONFIG_DIR/daemon.env
    UNIT_DST=$r/etc/systemd/system/$UNIT_NAME
    SUDOERS_DST=$r/etc/sudoers.d/fan-control
    LOGROTATE_DST=$r/etc/logrotate.d/thinkpad-fan-control
    MODPROBE_DIR=$r/etc/modprobe.d
    MODPROBE_CONF=$MODPROBE_DIR/thinkpad_acpi.conf
    LOG=$r/var/log/thinkpad-fan-control.log
    STATE_LIB_DIR=$r/var/lib/thinkpad-fan-control
    BACKUP_ROOT=$r/var/backups/thinkpad-fan-control
    RUN_DIR=$r/run/thinkpad-fan-control
    STATE_JSON=$RUN_DIR/state.json
    # Directly under /run (root-only 0755), not /run/lock (world-writable,
    # where a planted symlink could redirect a root open), and not inside
    # RUN_DIR, which systemd deletes whenever the daemon stops.
    LOCK_FILE=$r/run/thinkpad-fan-control.install.lock
    FAN_PROC=$r/proc/acpi/ibm/fan
    KERNEL_CMDLINE=$r/proc/cmdline
    TMP_PARENT=$r/tmp
}
set_paths ""

# reset_state — per-run mutable state, reset at the start of every run so a
# test can rehearse several runs in one shell.
reset_state() {
    STAMP=$(date +%Y%m%d-%H%M%S)
    BACKUP_DIR=$BACKUP_ROOT/$STAMP
    WORK="" STAGE=""
    TARGET_USER="" TARGET_UID="" TARGET_GID="" TARGET_HOME=""
    PHASE=preflight            # preflight | system | finishing | done
    MAIN_PID=$BASHPID          # the rollback runs only in this process
    UNIT_EXISTED=0 CONFIG_EXISTED=0 FIRST_INSTALL=0
    SERVICE_WAS_ACTIVE=0 SERVICE_WAS_ENABLED=0 SERVICE_TOUCHED=0
    NEED_REBOOT=0 REBOOT_REASON=""
    DAEMON_CHANGED=0 UNIT_CHANGED=0 ENV_CHANGED=0
    RESTART_EPOCH=0 NEW_VERSION="" UNDOING=0
    declare -g -a REPLACED=()          # paths changed this run, in order
    declare -g -A REPLACED_SEEN=()
    declare -g -a CREATED_DIRS=()      # dirs this run created
}
OPT_USER=""

# ── Output ───────────────────────────────────────────────────────────────────
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
step() { printf '\n%s%s. %s%s\n' "$BOLD" "$1" "$2" "$NC"; }

# die MESSAGE — abort the run. Once the system is being changed a die is a
# failure like any other and undoes what this run did. Inside a subshell
# (a $(...) calling a helper) it only exits: the parent sees the failure and
# runs the one rollback, in the one process that owns the run's state.
die() {
    err "$@"
    if [[ $BASHPID == "${MAIN_PID:-}" && ${PHASE:-} == system ]]; then
        undo_system_changes
    fi
    exit 1
}

usage() {
    cat <<EOF
Usage: sudo $0 [--user NAME]

Installs or upgrades ThinkPad Fan Control: the daemon, its root wrappers, the
sudoers rules, the systemd unit, the logrotate snippet and the desktop
entries. Safe to re-run; it restarts the daemon only when the daemon binary,
the unit or daemon.env changed. Undone again by: sudo $SRC/uninstall.sh

  --user NAME   the desktop user who runs the GUI (default: \$SUDO_USER,
                then logname)
EOF
}

# ── END SECTION 1 ──
# =============================================================================
# Pure helpers: no side effects beyond their arguments and stdout/stderr.
# Covered by the offline tests.
# =============================================================================

# compile_check FILE PYC_DIR — the contract's `python3 -m py_compile` check,
# but with the bytecode written into PYC_DIR: -m py_compile would drop a
# root-owned __pycache__ into the user's checkout. Prints the error, returns 1.
compile_check() {
    python3 - "$1" "$2/$(basename "$1").pyc" <<'PY'
import py_compile, sys
try:
    py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)
except py_compile.PyCompileError as e:
    msg = str(e.msg)
    sys.stderr.write(msg if msg.endswith("\n") else msg + "\n")
    sys.exit(1)
PY
}

# daemon_version FILE — print the module-level VERSION string of the daemon
# and succeed only for a 2.x build. Read with ast, never executed: running an
# unknown daemon as root just to ask its version could start a second fan
# controller. A v1 daemon (no READY=1) under the v2 Type=notify unit would
# hang `systemctl start` for 90 s and then be rolled back, hence the check.
daemon_version() {
    python3 - "$1" <<'PY'
import ast, re, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        tree = ast.parse(f.read())
except (OSError, SyntaxError, ValueError):
    sys.exit(1)
for node in tree.body:
    if isinstance(node, ast.Assign):
        targets, value = node.targets, node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets, value = [node.target], node.value
    else:
        continue
    for t in targets:
        if (isinstance(t, ast.Name) and t.id == "VERSION"
                and isinstance(value, ast.Constant) and isinstance(value.value, str)):
            print(value.value)
            sys.exit(0 if re.fullmatch(r"2\.\d+\.\d+", value.value) else 1)
sys.exit(1)
PY
}

# is_riff_wave FILE — the fallback alert is played by paplay as the user at
# the worst possible moment; an empty file or a git-lfs pointer must be
# caught here, not then.
is_riff_wave() {
    [[ -s $1 ]] || return 1
    [[ $(head -c 4 "$1") == RIFF && $(head -c 12 "$1" | tail -c 4) == WAVE ]]
}

# verify_unit UNIT_FILE SCRATCH_DIR — systemd-analyze verify on a copy that
# carries the final unit name (the check derives the unit type and name from
# the file name). On a first install ExecStart= does not exist yet; that one
# complaint is tolerated while the binary is really absent, nothing else is.
verify_unit() {
    local unit=$1 dir="$2/unit-verify" out rc=0 rest
    mkdir -p "$dir"
    cp "$unit" "$dir/$UNIT_NAME"
    out=$(systemd-analyze verify "$dir/$UNIT_NAME" 2>&1) || rc=$?
    if (( rc != 0 )) && [[ ! -e $UNIT_EXEC ]]; then
        rest=$(grep -v -F "Command $UNIT_EXEC is not executable" <<<"$out" || true)
        if [[ -z $rest ]]; then rc=0; fi
    fi
    if (( rc != 0 )); then printf '%s\n' "$out" >&2; fi
    return "$rc"
}

# user_name_ok NAME — the name lands in sudoers and in a sed replacement, so
# only the conventional shape is accepted (no metacharacters possible).
user_name_ok() {
    [[ $1 =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]
}

# render_sudoers TEMPLATE USER — the template names $TEMPLATE_USER at the
# start of each rule line; swap it for USER. "@TARGET_USER@" is accepted as a
# placeholder too. Defaults! lines and comments pass through untouched.
render_sudoers() {
    local template=$1 user=$2
    user_name_ok "$user" || return 1
    sed -E -e "s/^${TEMPLATE_USER}([[:space:]])/${user}\\1/" \
           -e "s/@TARGET_USER@/${user}/g" "$template"
}

# sudoers_command_ok CMD — one command of a rule, against the contract's list
# (§12). visudo only checks syntax: it would happily accept "NOPASSWD: ALL"
# or /bin/bash, and a template mistake must never become a root shell. The
# three wrappers validate their own arguments, so any argument pattern is
# fine for them; ryzenadj and systemctl are pinned to their exact arguments.
sudoers_command_ok() {
    local c=$1
    [[ $c =~ ^/usr/local/bin/(fan-set-level|fan-config-save|ryzenadj-set-tdp)\.sh([[:space:]].*)?$ ]] && return 0
    [[ $c == "/usr/local/bin/ryzenadj --info" ]] && return 0
    [[ $c =~ ^/usr/bin/systemctl[[:space:]]+(start|stop|restart)[[:space:]]+thinkpad-fan-control\.service$ ]] && return 0
    return 1
}

# sudoers_rules_ok FILE USER — content check on top of visudo. Accepted:
# blank lines and comments (not #include/@include), rules
# "USER ALL=(root) NOPASSWD: cmd, cmd" whose every command passes
# sudoers_command_ok, and "Defaults!<allowed cmd path> !syslog"-style lines
# that only switch logging off. Every rejected line is printed on stderr.
sudoers_rules_ok() {
    local file=$1 user=$2 line c rc=0 rules=0
    local rule_re="^${user}[[:space:]]+ALL[[:space:]]*=[[:space:]]*\\(root([[:space:]]*:[[:space:]]*root)?\\)[[:space:]]*NOPASSWD:[[:space:]]*(.+)$"
    local defaults_re='^Defaults!(/[^[:space:]]+)[[:space:]]+(!(syslog|logfile|log_input|log_output|log_allowed|log_denied)[[:space:]]*,?[[:space:]]*)+$'
    local -a cmds
    while IFS= read -r line || [[ -n $line ]]; do
        line=${line%$'\r'}
        if [[ $line =~ ^[[:space:]]*[#@]include ]]; then
            echo "include directive not allowed: $line" >&2; rc=1; continue
        fi
        [[ $line =~ ^[[:space:]]*(#|$) ]] && continue
        if [[ $line == *\\* ]]; then
            echo "backslash escapes not allowed: $line" >&2; rc=1; continue
        fi
        if [[ $line == Defaults* ]]; then
            # Logging switched off for one of the allowed commands is harmless;
            # any other Defaults (env_keep, !requiretty, ...) is not ours.
            if [[ $line =~ $defaults_re ]]; then
                c=${BASH_REMATCH[1]}
                [[ $c =~ ^(/usr/local/bin/(fan-set-level\.sh|fan-config-save\.sh|ryzenadj-set-tdp\.sh|ryzenadj)|/usr/bin/systemctl)$ ]] && continue
            fi
            echo "unexpected Defaults line: $line" >&2; rc=1
            continue
        fi
        if [[ ! $line =~ $rule_re ]]; then
            echo "unexpected rule: $line" >&2; rc=1; continue
        fi
        rules=$((rules + 1))
        IFS=',' read -r -a cmds <<<"${BASH_REMATCH[2]}"
        for c in "${cmds[@]}"; do
            c=${c#"${c%%[![:space:]]*}"}; c=${c%"${c##*[![:space:]]}"}
            if ! sudoers_command_ok "$c"; then
                echo "command not on the allow-list ('$c') in: $line" >&2; rc=1
            fi
        done
    done < "$file"
    if (( rules == 0 )); then echo "no rules for $user" >&2; rc=1; fi
    return "$rc"
}

# env_unquote RAW — the value a daemon.env right-hand side stands for, under
# the rules both readers share (systemd EnvironmentFile= and bash `.`):
# surrounding whitespace dropped; "…" with \\ \" \$ \` unescaped; '…' verbatim.
env_unquote() {
    local v=$1 out="" c n i
    v=${v#"${v%%[![:space:]]*}"}; v=${v%"${v##*[![:space:]]}"}
    if (( ${#v} >= 2 )) && [[ $v == \"*\" ]]; then
        v=${v:1:${#v}-2}
        for (( i = 0; i < ${#v}; i++ )); do
            c=${v:i:1}
            if [[ $c == '\' ]] && (( i + 1 < ${#v} )); then
                n=${v:i+1:1}
                case $n in
                    '\'|'"'|'$'|'`') out+=$n; i=$((i + 1)); continue ;;
                esac
            fi
            out+=$c
        done
        v=$out
    elif (( ${#v} >= 2 )) && [[ $v == \'*\' ]]; then
        v=${v:1:${#v}-2}
    fi
    printf '%s' "$v"
}

# env_quote VALUE — daemon.env is read by systemd AND by bash, so a value
# with anything beyond a conservative character set is double-quoted with
# \ " $ ` escaped: both parsers then yield the same string, and bash never
# expands ~, globs or $.
env_quote() {
    local v=$1
    if [[ $v =~ ^[A-Za-z0-9_./:@%+,=-]*$ ]]; then
        printf '%s' "$v"
    else
        v=${v//\\/\\\\}; v=${v//\"/\\\"}; v=${v//\$/\\\$}; v=${v//\`/\\\`}
        printf '"%s"' "$v"
    fi
}

# env_value_valid KEY VALUE — is an existing (unquoted) value worth keeping?
# The TDP rule is the one ryzenadj-set-tdp.sh and fanlib apply
# (^[1-9][0-9]?$), so all three agree on what a preserved value means; a
# leading zero would even be octal in the wrapper's (( )) comparison.
env_value_valid() {
    local key=$1 v=$2
    [[ $v != *[[:cntrl:]]* ]] || return 1
    case $key in
        FANCTL_GUI_UID|FANCTL_GUI_GID)
            [[ $v =~ ^[0-9]{1,10}$ ]] && (( 10#$v > 0 )) ;;
        FANCTL_GUI_HOME|FANCTL_GUI_RUNTIME_DIR)
            [[ $v == /* ]] ;;
        FANCTL_TDP_MAX|FANCTL_TDP_MAX_VRM_UNLOCKED)
            [[ $v =~ ^[1-9][0-9]?$ ]] ;;
        *)  return 1 ;;
    esac
}

# merge_daemon_env EXISTING UID GID HOME — print the new daemon.env (§2):
# exactly the six keys in order; a valid value already in EXISTING wins,
# computed defaults fill the gaps. Invalid values and unknown keys are
# reported on stderr (the old file stays in the backup). No timestamp in the
# header: the output must be byte-stable so cmp can decide about a restart.
merge_daemon_env() {
    local existing=$1 uid=$2 gid=$3 home=$4 line key val k
    local -A cur=() def=()
    def[FANCTL_GUI_UID]=$uid
    def[FANCTL_GUI_GID]=$gid
    def[FANCTL_GUI_HOME]=$home
    def[FANCTL_GUI_RUNTIME_DIR]=/run/user/$uid
    def[FANCTL_TDP_MAX]=$ENV_DEFAULT_TDP_MAX
    def[FANCTL_TDP_MAX_VRM_UNLOCKED]=$ENV_DEFAULT_TDP_MAX_VRM_UNLOCKED

    if [[ -r $existing ]]; then
        while IFS= read -r line || [[ -n $line ]]; do
            line=${line%$'\r'}
            [[ $line =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
            key=${BASH_REMATCH[2]}
            val=$(env_unquote "${BASH_REMATCH[3]}")
            if [[ -z ${def[$key]+set} ]]; then
                echo "daemon.env: dropping unknown key $key (kept in the backup)" >&2
            elif env_value_valid "$key" "$val"; then
                cur[$key]=$val          # the last assignment wins, as in both readers
            else
                echo "daemon.env: replacing invalid $key='$val' with ${def[$key]}" >&2
            fi
        done < "$existing"
    fi

    cat <<'HDR'
# ThinkPad Fan Control — daemon environment (docs/CONTRACT.md §2).
# Written by install.sh; values already present are preserved on upgrade.
# Read by the unit (EnvironmentFile=) and by ryzenadj-set-tdp.sh (set -a; .).
HDR
    for k in "${ENV_KEYS[@]}"; do
        printf '%s=%s\n' "$k" "$(env_quote "${cur[$k]-${def[$k]}}")"
    done
}

# ── END SECTION 2 ──
# desktop_key FILE KEY — print the value of KEY in the [Desktop Entry] group
# (the spec allows spaces around "="); return 1 when the key is absent, which
# for X-GNOME-Autostart-enabled means something else than an empty value.
desktop_key() {
    [[ -r $1 ]] || return 1
    awk -v k="$2" '
        /^[[:space:]]*\[/ { grp = $0; sub(/[[:space:]]+$/, "", grp); next }
        grp == "[Desktop Entry]" && index($0, k) == 1 {
            rest = substr($0, length(k) + 1)
            if (rest ~ /^[[:space:]]*=/) {
                sub(/^[[:space:]]*=[[:space:]]*/, "", rest); print rest; found = 1; exit
            }
        }
        END { exit !found }' "$1"
}

# autostart_flags FILE — "<enabled> <hidden>" as the session manager reads
# the existing entry: a missing X-GNOME-Autostart-enabled means enabled, a
# value other than true/1 means disabled; Hidden is true only for true/1.
# This is what keeps a re-run from switching on an autostart the user turned
# off (audit finding 17). Only call it for an existing file.
autostart_flags() {
    local file=$1 enabled=true hidden=false v
    if v=$(desktop_key "$file" X-GNOME-Autostart-enabled); then
        case $v in true|1) enabled=true ;; *) enabled=false ;; esac
    fi
    if v=$(desktop_key "$file" Hidden); then
        case $v in true|1) hidden=true ;; esac
    fi
    printf '%s %s\n' "$enabled" "$hidden"
}

# autostart_extra_lines FILE — the X-* keys of the existing entry other than
# X-GNOME-Autostart-enabled (e.g. X-GNOME-Autostart-Delay set in Startup
# Applications), carried over so a rewrite keeps them.
autostart_extra_lines() {
    [[ -r $1 ]] || return 0
    awk '
        /^[[:space:]]*\[/ { grp = $0; sub(/[[:space:]]+$/, "", grp); next }
        grp == "[Desktop Entry]" && /^X-[A-Za-z0-9-]+[[:space:]]*=/ &&
            !/^X-GNOME-Autostart-enabled[[:space:]]*=/ { print }' "$1"
}

# desktop_path_ok PATH — Exec=/Path=/Icon= are written without any escaping;
# a checkout path with characters that would need it (quotes, $, `, \, %,
# control characters) is refused rather than half-escaped. Spaces are fine
# (Exec quotes the argument).
desktop_path_ok() {
    [[ $1 != *[\"\'\$\`\\%]* && $1 != *[[:cntrl:]]* ]]
}

# render_desktop_entry SRC [ENABLED HIDDEN [EXTRA_LINES]] — the launcher, or
# with the flags the autostart variant (starts in the tray). The body is the
# v1 text on purpose: on an upgrade the autostart file only changes where the
# user's own choices say so. X-GNOME-Autostart-enabled stays on a line of its
# own followed by Hidden=; fanlib's autostart switch rewrites that line only.
render_desktop_entry() {
    local src=$1 exec_path=$1/app.py suffix=""
    if [[ $exec_path == *[[:space:]]* ]]; then exec_path="\"$exec_path\""; fi
    if (( $# >= 3 )); then suffix=" --tray"; fi
    cat <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Fan Control
GenericName=ThinkPad Fan Control
Comment=Monitor and control ThinkPad fan speeds, temperatures and CPU power limits
Exec=python3 $exec_path$suffix
Path=$src
Icon=$src/icon.png
Terminal=false
Categories=System;HardwareSettings;Monitor;
Keywords=fan;temperature;cooling;thinkpad;rpm;tdp;
StartupNotify=true
EOF
    if (( $# >= 3 )); then
        printf 'X-GNOME-Autostart-enabled=%s\nHidden=%s\n' "$2" "$3"
        if [[ -n ${4:-} ]]; then printf '%s\n' "$4"; fi
    fi
}

# rotate_log_once LOG — one-time migration of an oversized v1 log (§18.5):
# LOG -> LOG.1 (NUL runs from past hard crashes stripped) -> LOG.1.gz. An
# older generation shifts to LOG.2.gz first (logrotate's "rotate 2" would drop
# anything older at its next run anyway). Both an uncompressed LOG.1 and a
# LOG.1.gz is not a state logrotate leaves; then nothing is moved.
# Returns 0 rotated, 1 not needed, 2 skipped or a step failed (every line is
# still in LOG, LOG.1 or LOG.1.gz). Each step checks itself because callers
# use this in a condition, where set -e is off. The caller recreates LOG; the
# daemons (v1 and v2) reopen the log per line, so no signal is needed.
rotate_log_once() {
    local log=$1 size
    [[ -f $log ]] || return 1
    size=$(stat -c %s "$log") || return 2
    (( size > LOG_ROTATE_BYTES )) || return 1
    if [[ -e $log.1 && -e $log.1.gz ]]; then
        return 2
    elif [[ -f $log.1 ]]; then
        { tr -d '\000' < "$log.1" | gzip -c > "$log.2.gz.tmp"; } || return 2
        { mv -f "$log.2.gz.tmp" "$log.2.gz" && rm -f "$log.1"; } || return 2
    elif [[ -f $log.1.gz ]]; then
        mv -f "$log.1.gz" "$log.2.gz" || return 2
    fi
    mv -f "$log" "$log.1" || return 2
    tr -d '\000' < "$log.1" > "$log.1.tmp" || return 2
    mv -f "$log.1.tmp" "$log.1" || return 2
    gzip -f "$log.1" || return 2
    return 0
}

# state_fresh STATE_JSON [NOT_BEFORE] — the §5 liveness rule (parses, and
# time.time() - ts < 3*sample_interval + 2), plus ts >= NOT_BEFORE so a file
# left by the previous process can never pass for the new one. Prints a
# one-line summary with level/reason/temp_raw/rpm when present.
state_fresh() {
    python3 - "$1" "${2:-0}" <<'PY'
import json, sys, time
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
    ts = float(d["ts"])
except Exception as e:                       # absent, half-written, wrong shape
    print(f"state.json unreadable ({type(e).__name__}: {e})")
    sys.exit(1)
si = d.get("sample_interval")
si = si if isinstance(si, int) and not isinstance(si, bool) and si >= 1 else 1
age = time.time() - ts
parts = [f"v{d['version']}"] if d.get("version") is not None else []
for key in ("level", "reason", "temp_raw", "rpm"):
    if d.get(key) is not None:
        parts.append(f"{key} {d[key]}")
parts.append(f"age {age:.1f} s")
print(" · ".join(parts))
sys.exit(0 if age < 3 * si + 2 and ts >= float(sys.argv[2]) else 1)
PY
}

# state_version STATE_JSON — "version" of a fresh state.json, else nothing.
state_version() {
    python3 - "$1" <<'PY'
import json, sys, time
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
    si = d.get("sample_interval")
    si = si if isinstance(si, int) and not isinstance(si, bool) and si >= 1 else 1
    if time.time() - float(d["ts"]) < 3 * si + 2 and isinstance(d.get("version"), str):
        print(d["version"])
except Exception:
    pass
PY
}

# active_hold STATE_JSON — "level X, M min S s left" and success when the
# running daemon reports a hold (§5 override) in a FRESH state.json (a
# stale file says nothing about what the process holds now); a restart
# drops it (§7).
active_hold() {
    python3 - "$1" <<'PY'
import json, sys, time
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
    si = d.get("sample_interval")
    si = si if isinstance(si, int) and not isinstance(si, bool) and si >= 1 else 1
    o = d.get("override")
    if not isinstance(o, dict) or time.time() - float(d["ts"]) >= 3 * si + 2:
        sys.exit(1)
    left = max(0, int(float(o.get("until", 0)) - time.time()))
except Exception:
    sys.exit(1)
print(f'level {o.get("level")}, {left // 60} min {left % 60} s left')
PY
}

# summarize_save_config OUTPUT — check and pretty-print the reply of
# `--save-config`: its LAST non-empty stdout line must be
# {"config": {...}, "rejected": [...]} (§3). Returns 1 on any other shape.
summarize_save_config() {
    python3 - "$1" <<'PY'
import json, sys
lines = [l for l in sys.argv[1].splitlines() if l.strip()]
try:
    d = json.loads(lines[-1])
    cfg, rejected = d["config"], d["rejected"]
    assert isinstance(cfg, dict) and isinstance(rejected, list)
except Exception:
    sys.exit(1)
def curve(steps):
    try:
        return " ".join((f'{s["temp"]}/{s["down"]}' if "down" in s else str(s["temp"])) + f':{s["level"]}'
                        for s in steps)
    except Exception:
        return "?"
print(f'    schema {cfg.get("schema")} · sample {cfg.get("sample_interval")} s · '
      f'watchdog {cfg.get("watchdog")} s · critical {cfg.get("critical_temp")} °C · '
      f'hysteresis {cfg.get("hysteresis")} · dwell {cfg.get("dwell_down_s")} s')
print("    AC curve:      " + curve(cfg.get("curve") or []))
print("    battery curve: " + curve(cfg.get("battery_curve") or []))
if rejected:
    print("    rejected keys: " + ", ".join(map(str, rejected)))
PY
}

# fan_control_active FAN_PROC — is thinkpad_acpi running with fan_control=1?
# The driver prints the "commands: level <level> ..." line only then. The
# "status:" line is no indicator: "disabled" there means the EC fan register
# is 0, i.e. the fan sits at level 0, whatever the module option says.
fan_control_active() {
    grep -qE '^commands:[[:space:]]*level <level>' "$1" 2>/dev/null
}

# fan_status_word FAN_PROC — the raw "status:" word, for information only.
fan_status_word() {
    sed -n 's/^status:[[:space:]]*//p' "$1" 2>/dev/null | head -n 1 || true
}

# modprobe_has_fan_control DIR CMDLINE — is fan_control=1 already configured
# for the next boot, in any DIR/*.conf or on the kernel command line? Checking
# every file avoids a second options line when the user or another tool
# (thinkfan, for one) already set it. modprobe treats - and _ in module names
# alike; kernel bools also accept y/Y.
modprobe_has_fan_control() {
    local dir=$1 cmdline=$2 f
    grep -qsE '(^|[[:space:]])thinkpad[-_]acpi\.fan_control=(1|y|Y)([[:space:]]|$)' "$cmdline" && return 0
    for f in "$dir"/*.conf; do
        [[ -f $f ]] || continue
        grep -qE '^[[:space:]]*options[[:space:]]+thinkpad[-_]acpi([[:space:]].*)?[[:space:]]fan_control=(1|y|Y)([[:space:]]|$)' "$f" && return 0
    done
    return 1
}

# modprobe_conf_with_fan_control FILE — print FILE (may be absent) with
# fan_control=1 set: an existing fan_control= value on the first
# "options thinkpad_acpi" line is replaced, else the option is appended to
# that line, else a marked options line is added.
modprobe_conf_with_fan_control() {
    local file=$1
    [[ -f $file ]] || file=/dev/null
    awk -v marker="$MODPROBE_MARKER" '
        !done && /^[[:space:]]*options[[:space:]]+thinkpad[-_]acpi([[:space:]]|$)/ {
            if ($0 ~ /[[:space:]]fan_control=[^[:space:]]*/) gsub(/fan_control=[^[:space:]]*/, "fan_control=1")
            else $0 = $0 " fan_control=1"
            done = 1
        }
        { print }
        END { if (!done) { print marker; print "options thinkpad_acpi fan_control=1" } }' "$file"
}

# gui_pids USER SRC — pids of Fan Control GUIs (app.py / server.py) of the
# checkout SRC running as USER, one per line. Only Python interpreters count
# (an editor or a shell whose command line merely mentions the file does
# not). A script argument is resolved against the process's cwd, so the
# desktop-entry form (absolute path), launch.sh, `python3 app.py` inside the
# checkout (the v1 launcher), `./app.py` and `python3 fan-gui/app.py` from
# $HOME all count, while another project's app.py does not. The contract's
# literal pattern (an argument containing "fan-gui/app.py") also counts: for
# a notice, a false positive costs nothing. Read-only.
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

# has_blanket_nopasswd SUDO_L_OUTPUT — does `sudo -l -U user` list a
# password-less rule for ALL commands? With one, anything running as the user
# can become root directly, and the wrapper allow-list protects nothing
# (audit finding 14). Only ever used for a warning.
has_blanket_nopasswd() {
    grep -qE 'NOPASSWD:[[:space:]]*ALL[[:space:]]*$' <<<"$1"
}

# ── END SECTION 3 ──
# =============================================================================
# System helpers: these change the machine.
# =============================================================================

# own PATH… — root ownership for something this run created (a no-op in the
# offline rehearsal, which runs without root).
own() {
    if [[ -n $OWN_SPEC ]]; then chown "$OWN_SPEC" -- "$@"; fi
}

# backup_copy PATH — copy PATH into this run's backup tree, preserving mode,
# owner and timestamps (and a symlink as a symlink).
backup_copy() {
    local p=$1
    mkdir -p -- "$BACKUP_DIR$(dirname -- "$p")"
    cp -a -- "$p" "$BACKUP_DIR$p"
    say "backed up $p"
}

# backup PATH — back up once per run and register PATH for rollback. A path
# that does not exist yet is registered too: a rollback removes it again.
backup() {
    local p=$1
    [[ -z ${REPLACED_SEEN[$p]:-} ]] || return 0
    REPLACED_SEEN[$p]=1
    REPLACED+=("$p")
    if [[ -e $p || -L $p ]]; then backup_copy "$p"; fi
    return 0
}

# forget_backup_if_unchanged PATH — PATH was rewritten but came out
# byte-identical (config.json after --save-config on a re-run): drop its
# backup copy and its rollback entry, so an idempotent re-run leaves no
# backup directory behind.
forget_backup_if_unchanged() {
    local p=$1 d i
    local -a keep=()
    [[ -f $BACKUP_DIR$p && -f $p ]] || return 0
    cmp -s -- "$BACKUP_DIR$p" "$p" || return 0
    rm -f -- "$BACKUP_DIR$p"
    d=$(dirname -- "$BACKUP_DIR$p")
    while [[ $d == "$BACKUP_DIR"/* ]] && rmdir -- "$d" 2>/dev/null; do
        d=$(dirname -- "$d")
    done
    for i in "${REPLACED[@]}"; do
        if [[ $i != "$p" ]]; then keep+=("$i"); fi
    done
    REPLACED=("${keep[@]}")
    unset 'REPLACED_SEEN[$p]'
    return 0
}

# ensure_dir DIR — create DIR (and missing parents) 0755 root, remembering
# every component this run created so a rollback can remove them again.
ensure_dir() {
    local d=$1 i
    local -a missing=()
    while [[ ! -d $d ]]; do
        missing+=("$d")
        d=$(dirname -- "$d")
    done
    for (( i = ${#missing[@]} - 1; i >= 0; i-- )); do
        mkdir -m 0755 -- "${missing[$i]}"
        own "${missing[$i]}"
        CREATED_DIRS+=("${missing[$i]}")
    done
}

# install_file SRC DST MODE — install under a dot-name next to DST, then
# rename: sudo, systemd or the shell may read DST at any moment and must
# never see it half-written. sudo also skips dot-names in sudoers.d.
install_file() {
    local src=$1 dst=$2 mode=$3 tmp
    tmp="$(dirname -- "$dst")/.$(basename -- "$dst").fanctl-new"
    install -m "$mode" "${OWN_ARGS[@]}" -- "$src" "$tmp"
    mv -f -- "$tmp" "$dst"
}

# rollback_one PATH — put back the backed-up copy (atomically: the old
# daemon may be reading config.json while this runs), or remove PATH when it
# did not exist before this run.
rollback_one() {
    local p=$1 tmp
    if [[ -e $BACKUP_DIR$p || -L $BACKUP_DIR$p ]]; then
        tmp="$(dirname -- "$p")/.$(basename -- "$p").fanctl-restore"
        if cp -a -- "$BACKUP_DIR$p" "$tmp" && mv -f -- "$tmp" "$p"; then
            say "restored $p"
        else
            err "could not restore $p; the saved copy is $BACKUP_DIR$p"
        fi
    elif [[ -e $p || -L $p ]]; then
        if rm -f -- "$p"; then say "removed $p (it did not exist before this run)"; fi
    fi
}

# undo_service_if_new — a unit this run created is stopped and disabled
# before its file disappears, or the enable symlink would dangle. Decided by
# UNIT_EXISTED (recorded in preflight), not by the presence of a backup: an
# unchanged unit file is never backed up.
undo_service_if_new() {
    if (( ! UNIT_EXISTED )) && [[ -e $UNIT_DST ]]; then
        systemctl disable -q --now "$UNIT_NAME" 2>/dev/null
    fi
    return 0
}

# restore_service_state — with the old files back, return the unit to the
# state it had before this run: running if it was (restarted if this run
# restarted it or it died meanwhile), stopped if it was stopped, disabled if
# it was disabled.
restore_service_state() {
    if [[ ! -e $UNIT_DST ]]; then
        if (( SERVICE_TOUCHED )); then
            warn "first install undone: no daemon is installed, the fan stays under firmware control"
        fi
        return 0
    fi
    systemctl reset-failed "$UNIT_NAME" 2>/dev/null
    if (( ! SERVICE_WAS_ENABLED )); then
        systemctl disable -q "$UNIT_NAME" 2>/dev/null
    fi
    if (( SERVICE_WAS_ACTIVE )); then
        if (( SERVICE_TOUCHED )) || ! systemctl is-active --quiet "$UNIT_NAME"; then
            if systemctl restart "$UNIT_NAME"; then
                ok "previous daemon restarted"
            else
                err "the previous daemon did not come back. The fan is under firmware control (a stopping daemon writes 'level auto', and the EC watchdog reverts a level left behind). See: journalctl -u $UNIT_NAME -n 30"
            fi
        fi
    elif (( SERVICE_TOUCHED )); then
        systemctl stop "$UNIT_NAME" 2>/dev/null
        say "daemon stopped again (it was not running before this run)"
    fi
    return 0
}

# undo_system_changes — the single rollback path (ERR trap, die, signal,
# failed post-check): files newest first, then the directories this run
# created, then the service state. set +e so one failed restore does not
# abandon the others; signals are ignored so a second Ctrl-C cannot cut the
# rollback short.
undo_system_changes() {
    local i
    (( ! UNDOING )) || return 0
    UNDOING=1
    trap - ERR
    trap '' INT TERM HUP
    set +e
    warn "Undoing the changes made by this run…"
    undo_service_if_new
    for (( i = ${#REPLACED[@]} - 1; i >= 0; i-- )); do
        rollback_one "${REPLACED[$i]}"
    done
    for (( i = ${#CREATED_DIRS[@]} - 1; i >= 0; i-- )); do
        if rmdir -- "${CREATED_DIRS[$i]}" 2>/dev/null; then say "removed ${CREATED_DIRS[$i]}"; fi
    done
    systemctl daemon-reload 2>/dev/null
    restore_service_state
    if [[ -d $BACKUP_DIR ]]; then say "this run's backups stay in $BACKUP_DIR"; fi
    PHASE=undone
    return 0
}

# on_err — ERR trap. In a subshell it only propagates the failure; the main
# process decides what to undo.
on_err() {
    local rc=$? line=${BASH_LINENO[0]:-?} cmd=${BASH_COMMAND:-?}
    if [[ $BASHPID != "$MAIN_PID" ]]; then exit "$rc"; fi
    trap - ERR
    echo
    err "install.sh failed at line $line (exit $rc): $cmd"
    case $PHASE in
        system)
            undo_system_changes ;;
        finishing)
            warn "The daemon part of the install completed and passed its post-check; only the step above failed."
            if [[ -d $BACKUP_DIR ]]; then warn "Backups: $BACKUP_DIR"; fi ;;
    esac
    exit 1
}

on_signal() {
    if [[ $BASHPID != "$MAIN_PID" ]]; then exit 130; fi
    trap - ERR
    echo
    err "interrupted ($1)"
    if [[ $PHASE == system ]]; then undo_system_changes; fi
    exit 130
}

cleanup() {
    if [[ -n ${WORK:-} && -d $WORK ]]; then rm -rf -- "$WORK"; fi
    return 0
}

# acquire_lock — install.sh and uninstall.sh never run concurrently.
acquire_lock() {
    exec 9>>"$LOCK_FILE" || die "cannot open the lock file $LOCK_FILE"
    flock -n 9 || die "another install.sh or uninstall.sh is running (lock: $LOCK_FILE)"
}

# as_user CMD… — run as the desktop user. Everything under $HOME is written
# this way: a root write into a user-owned directory can be redirected by a
# symlink planted there, and the files come out owned by the user.
as_user() {
    runuser -u "$TARGET_USER" -- "$@"
}

# read_user_file PATH OUT — copy a file from the user's home into OUT, read
# AS the user: root never opens a path under $HOME itself, so a symlink
# planted there cannot make it read (and later rewrite into a user-owned
# file) something only root may see. Fails when PATH is missing, not a
# regular file, or unreadable.
read_user_file() {
    # shellcheck disable=SC2016  # $1 belongs to the inner sh
    as_user /bin/sh -c '[ -f "$1" ] && exec cat -- "$1"' fanctl-read "$1" > "$2" 2>/dev/null
}

# backup_user_copy PATH — the backup of a file in the user's home (content
# only, read as the user). Best effort: a dangling link is noted, not fatal.
backup_user_copy() {
    local p=$1
    mkdir -p -- "$BACKUP_DIR$(dirname -- "$p")"
    if read_user_file "$p" "$BACKUP_DIR$p"; then
        say "backed up $p"
    else
        rm -f -- "$BACKUP_DIR$p"
        warn "could not back up $p (not a readable regular file); replacing it anyway"
    fi
}

# install_user_file SRC DST MODE — atomic write of a file in the user's home,
# done as the user. An identical file is left alone (only its mode is set),
# so a re-run does not reset desktop metadata such as a launcher's trust
# flag. Sets USER_FILE_RESULT to "unchanged" or "written".
install_user_file() {
    local src=$1 dst=$2 mode=$3
    # shellcheck disable=SC2016  # $1 belongs to the inner sh
    if as_user /bin/sh -c '[ -f "$1" ] && [ ! -L "$1" ] && cmp -s - "$1"' fanctl-cmp "$dst" < "$src"; then
        as_user chmod "$mode" -- "$dst"
        USER_FILE_RESULT=unchanged
        return 0
    fi
    # shellcheck disable=SC2016
    if as_user /bin/sh -c '[ -e "$1" ] || [ -L "$1" ]' fanctl-test "$dst"; then
        backup_user_copy "$dst"
    fi
    # shellcheck disable=SC2016  # $1/$2 belong to the inner sh
    as_user /bin/sh -c '
        set -e
        cd /
        umask 022
        dir=$(dirname -- "$1")
        tmp="$dir/.$(basename -- "$1").fanctl-tmp"
        mkdir -p -- "$dir"
        cat > "$tmp"
        chmod "$2" "$tmp"
        mv -f -- "$tmp" "$1"
    ' fanctl-install "$dst" "$mode" < "$src"
    USER_FILE_RESULT=written
}

# sudo_listing USER — what sudo grants USER (read-only; root never prompts).
sudo_listing() {
    sudo -n -l -U "$1" 2>/dev/null || true
}

# ── END SECTION 4 ──
# =============================================================================
# Steps
# =============================================================================

parse_args() {
    while (( $# )); do
        case $1 in
            --user)
                if (( $# < 2 )); then usage >&2; exit 2; fi
                OPT_USER=$2; shift 2 ;;
            --user=*) OPT_USER=${1#--user=}; shift ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; err "unknown argument: $1"; exit 2 ;;
        esac
    done
}

resolve_target_user() {
    local u=${OPT_USER:-${SUDO_USER:-}} ent
    if [[ -z $u ]]; then u=$(logname 2>/dev/null || true); fi
    [[ -n $u && $u != root ]] \
        || die "could not determine the desktop user: run through sudo from your desktop session, or pass --user NAME"
    user_name_ok "$u" || die "unusual user name '$u': refusing to write it into sudoers"
    ent=$(getent passwd "$u") || die "user '$u' does not exist"
    IFS=: read -r _ _ TARGET_UID TARGET_GID _ TARGET_HOME _ <<<"$ent"
    TARGET_USER=$u
    [[ $TARGET_UID =~ ^[0-9]+$ && $TARGET_GID =~ ^[0-9]+$ ]] || die "getent returned a non-numeric uid/gid for $u"
    (( TARGET_UID != 0 )) || die "$u has uid 0; the GUI must run as an unprivileged desktop user"
    [[ $TARGET_HOME == /* && -d $TARGET_HOME ]] || die "home directory '$TARGET_HOME' of $u does not exist"
}

# stage_sources — copy what gets installed into the private work dir. The
# checkout is user-writable (and may be edited while this runs): everything
# after this point checks and installs the staged bytes only.
stage_sources() {
    local f
    for f in daemon/thinkpad-fan-controld daemon/fan-set-level.sh daemon/fan-config-save.sh \
             ryzenadj-set-tdp.sh "daemon/$UNIT_NAME" daemon/fan-control.sudoers \
             daemon/thinkpad-fan-control.logrotate alert.wav; do
        [[ -f $SRC/$f ]] || die "missing $SRC/$f"
        mkdir -p -- "$STAGE/$(dirname -- "$f")"
        cp -- "$SRC/$f" "$STAGE/$f"
    done
    # Not installed, but the desktop entries start the GUI from the checkout.
    for f in fanlib.py app.py server.py index.html icon.png; do
        [[ -f $SRC/$f ]] || die "missing $SRC/$f"
    done
}

# check_kernel_fan_control — read-only; decides whether the daemon can be
# started now. The modprobe.d option itself is written in step 1.
check_kernel_fan_control() {
    local status
    if [[ ! -e $FAN_PROC ]]; then
        NEED_REBOOT=1
        REBOOT_REASON="$FAN_PROC does not exist: thinkpad_acpi is not loaded (is this a ThinkPad?)"
        warn "$REBOOT_REASON"
    elif ! fan_control_active "$FAN_PROC"; then
        NEED_REBOOT=1
        REBOOT_REASON="thinkpad_acpi is loaded without fan_control=1 ($FAN_PROC offers no 'commands: level' line)"
        warn "$REBOOT_REASON"
    else
        ok "fan control available ($FAN_PROC offers 'commands: level …')"
    fi
    status=$(fan_status_word "$FAN_PROC")
    if [[ $status == disabled ]]; then
        say "$FAN_PROC says 'status: disabled': the fan is at level 0 right now. That is neither an error nor the fan_control option."
    fi
    if (( NEED_REBOOT )); then
        warn "the daemon will be installed and enabled but NOT started; it starts after a reboot"
    fi
}

preflight() {
    local f
    step 0 "Preflight (nothing on the system changes until this passes)"
    resolve_target_user
    say "desktop user: $TARGET_USER (uid $TARGET_UID, gid $TARGET_GID, home $TARGET_HOME)"
    say "source: $SRC"

    WORK=$(mktemp -d "$TMP_PARENT/fanctl-install.XXXXXX")
    STAGE=$WORK/stage
    mkdir -p -- "$STAGE" "$WORK/pyc"
    stage_sources
    ok "source files present and staged"

    for f in "$STAGE/daemon/thinkpad-fan-controld" "$SRC/fanlib.py" "$SRC/app.py" "$SRC/server.py"; do
        compile_check "$f" "$WORK/pyc" || die "Python syntax error in ${f##*/} (see above)"
    done
    ok "Python byte-compiles (daemon, fanlib, app, server)"
    NEW_VERSION=$(daemon_version "$STAGE/daemon/thinkpad-fan-controld") \
        || die "daemon/thinkpad-fan-controld declares no VERSION = \"2.x.y\" (found '$NEW_VERSION'); the v2 unit (Type=notify) needs the v2 daemon"
    ok "daemon version $NEW_VERSION"

    for f in daemon/fan-set-level.sh daemon/fan-config-save.sh ryzenadj-set-tdp.sh; do
        bash -n "$STAGE/$f" || die "shell syntax error in $f (see above)"
    done
    ok "root wrappers parse (bash -n)"

    is_riff_wave "$STAGE/alert.wav" || die "alert.wav is empty or not a RIFF/WAVE file"
    grep -qF /var/log/thinkpad-fan-control.log "$STAGE/daemon/thinkpad-fan-control.logrotate" \
        || die "daemon/thinkpad-fan-control.logrotate does not name /var/log/thinkpad-fan-control.log"
    ok "alert.wav is a WAV file; logrotate snippet present"

    verify_unit "$STAGE/daemon/$UNIT_NAME" "$WORK" \
        || die "systemd-analyze verify rejected daemon/$UNIT_NAME (see above)"
    ok "unit passes systemd-analyze verify"

    render_sudoers "$STAGE/daemon/fan-control.sudoers" "$TARGET_USER" > "$STAGE/fan-control" \
        || die "could not render the sudoers template for $TARGET_USER"
    visudo -cf "$STAGE/fan-control" >/dev/null || die "the rendered sudoers file fails visudo -c (see above)"
    sudoers_rules_ok "$STAGE/fan-control" "$TARGET_USER" \
        || die "the rendered sudoers file grants something outside the contract's command list (see above)"
    ok "sudoers for $TARGET_USER passes visudo -c and the command allow-list"

    if [[ -e $UNIT_DST ]]; then UNIT_EXISTED=1; fi
    if [[ -e $CONFIG ]]; then CONFIG_EXISTED=1; fi
    if (( ! UNIT_EXISTED && ! CONFIG_EXISTED )); then FIRST_INSTALL=1; fi
    if systemctl is-active --quiet "$UNIT_NAME"; then SERVICE_WAS_ACTIVE=1; fi
    if systemctl is-enabled --quiet "$UNIT_NAME" 2>/dev/null; then SERVICE_WAS_ENABLED=1; fi
    if (( FIRST_INSTALL )); then
        say "first install (no unit, no config yet)"
    else
        say "upgrade: unit $( (( SERVICE_WAS_ACTIVE )) && echo active || echo inactive ), $( (( SERVICE_WAS_ENABLED )) && echo enabled || echo 'not enabled' )"
    fi
    check_kernel_fan_control
    ok "preflight passed"
}

ensure_modprobe_option() {
    if modprobe_has_fan_control "$MODPROBE_DIR" "$KERNEL_CMDLINE"; then
        ok "thinkpad_acpi fan_control=1 is configured for boot"
        return 0
    fi
    ensure_dir "$MODPROBE_DIR"
    modprobe_conf_with_fan_control "$MODPROBE_CONF" > "$WORK/thinkpad_acpi.conf"
    backup "$MODPROBE_CONF"
    install_file "$WORK/thinkpad_acpi.conf" "$MODPROBE_CONF" 0644
    ok "wrote fan_control=1 to $MODPROBE_CONF (applies whenever thinkpad_acpi is next loaded, i.e. at boot)"
}

# install_root_file SRC DST MODE — install when different (backing up the
# old copy); otherwise only re-assert owner and mode. Sets FILE_CHANGED.
install_root_file() {
    local src=$1 dst=$2 mode=$3
    FILE_CHANGED=0
    ensure_dir "$(dirname -- "$dst")"
    if [[ -f $dst && ! -L $dst ]] && cmp -s -- "$src" "$dst"; then
        own "$dst"
        chmod "$mode" -- "$dst"
        ok "$dst unchanged"
        return 0
    fi
    backup "$dst"
    install_file "$src" "$dst" "$mode"
    FILE_CHANGED=1
    ok "installed $dst"
}

install_binaries() {
    local f src
    for f in "${DAEMON_BINS[@]}"; do
        if [[ $f == ryzenadj-set-tdp.sh ]]; then src=$STAGE/$f; else src=$STAGE/daemon/$f; fi
        install_root_file "$src" "$BIN_DIR/$f" 0755
        if [[ $f == thinkpad-fan-controld ]]; then DAEMON_CHANGED=$FILE_CHANGED; fi
    done
    install_root_file "$STAGE/alert.wav" "$SHARE_DIR/alert.wav" 0644
    # The shell daemon that preceded v1, if this machine still has it.
    if [[ -e $BIN_DIR/thinkpad-fan-control.sh ]]; then
        backup "$BIN_DIR/thinkpad-fan-control.sh"
        rm -f -- "$BIN_DIR/thinkpad-fan-control.sh"
        say "retired the pre-v1 shell daemon (a copy is in the backup)"
    fi
}

write_daemon_env() {
    local line kept_uid kept_gid
    ensure_dir "$CONFIG_DIR"
    merge_daemon_env "$ENV_FILE" "$TARGET_UID" "$TARGET_GID" "$TARGET_HOME" \
        > "$WORK/daemon.env" 2> "$WORK/daemon.env.warnings"
    while IFS= read -r line; do warn "$line"; done < "$WORK/daemon.env.warnings"
    install_root_file "$WORK/daemon.env" "$ENV_FILE" 0644
    ENV_CHANGED=$FILE_CHANGED
    grep -v '^#' "$ENV_FILE" | sed 's/^/    /' || true
    kept_uid=$(sed -n 's/^FANCTL_GUI_UID=//p' "$ENV_FILE")
    kept_gid=$(sed -n 's/^FANCTL_GUI_GID=//p' "$ENV_FILE")
    if [[ $kept_uid != "$TARGET_UID" || $kept_gid != "$TARGET_GID" ]]; then
        warn "daemon.env keeps FANCTL_GUI_UID=$kept_uid / FANCTL_GUI_GID=$kept_gid (preserved) but $TARGET_USER is $TARGET_UID/$TARGET_GID; edit $ENV_FILE if the GUI cannot reach the daemon's socket"
    fi
}

# run_save_config — the new daemon's --save-config (§3) with stdin passed
# through. env -i: the daemon honours FANCTL_CONFIG_FILE / FANCTL_LOG_FILE
# test overrides, and a root shell carrying one (sudo -E, a stray export)
# must not redirect the migration.
run_save_config() {
    env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 \
        timeout "$SAVE_CONFIG_TIMEOUT" "$DAEMON" --save-config
}

# migrate_config — the daemon is the only writer of config.json: feeding it
# the existing file turns a v1 config into schema 2 (poll_interval dropped,
# watchdog 0 -> 60, v1 default curves -> v2 defaults, new keys added). A
# still-running v1 daemon hot-reloads the result once before step 7
# restarts it; v1 ignores the keys it does not know.
migrate_config() {
    local out rc=0
    ensure_dir "$CONFIG_DIR"
    backup "$CONFIG"
    if [[ -s $CONFIG ]]; then
        out=$(run_save_config < "$CONFIG") || rc=$?
        if (( rc != 0 )); then
            warn "the new daemon did not accept the existing config.json (exit $rc); normalising from '{}' instead. Your file is saved as $BACKUP_DIR$CONFIG"
            rc=0
            out=$(printf '{}\n' | run_save_config) || rc=$?
        fi
    else
        out=$(printf '{}\n' | run_save_config) || rc=$?
        if (( rc == 0 )); then ok "wrote the default config (first install)"; fi
    fi
    (( rc == 0 )) || die "thinkpad-fan-controld --save-config failed (exit $rc): the new daemon cannot write its config"
    own "$CONFIG"
    chmod 0644 -- "$CONFIG"
    if summarize_save_config "$out"; then
        ok "config.json normalised by the new daemon ($CONFIG)"
    else
        warn "--save-config did not print the expected {\"config\", \"rejected\"} line; check $CONFIG by hand"
        if [[ -n $out ]]; then printf '%s\n' "$out" | sed 's/^/    /'; fi
    fi
    forget_backup_if_unchanged "$CONFIG"
}

# ── END SECTION 5 ──
install_log_and_logrotate() {
    local rc=0 size
    ensure_dir "$(dirname -- "$LOG")"
    if [[ -f $LOG ]]; then
        size=$(stat -c %s -- "$LOG")
        if (( size > LOG_ROTATE_BYTES )); then
            # Not backed up: every line survives in LOG.1.gz, and a rollback
            # putting a multi-MB file back over the fresh log would only
            # duplicate it.
            rotate_log_once "$LOG" || rc=$?
            if (( rc == 0 )); then
                ok "rotated the $((size / 1024)) KiB log to $LOG.1.gz (NUL bytes stripped)"
            else
                warn "one-time log rotation skipped or incomplete; every line is still in $LOG, $LOG.1 or $LOG.1.gz, and logrotate takes over from here"
            fi
        else
            ok "$LOG kept ($size bytes)"
        fi
    fi
    # A daemon appending between the rotation and this line has already
    # re-created the file; it is kept, only its mode is corrected below.
    if [[ ! -e $LOG ]]; then
        install -m 0644 "${OWN_ARGS[@]}" /dev/null "$LOG"
        ok "created $LOG"
    fi
    # The v2 unit runs with UMask=0077; the user-side log viewer needs 0644.
    own "$LOG"
    chmod 0644 -- "$LOG"
    install_root_file "$STAGE/daemon/thinkpad-fan-control.logrotate" "$LOGROTATE_DST" 0644
}

install_sudoers() {
    # Checked in preflight; checked again as the exact file being moved in.
    visudo -cf "$STAGE/fan-control" >/dev/null || die "the rendered sudoers file fails visudo -c"
    install_root_file "$STAGE/fan-control" "$SUDOERS_DST" 0440
}

install_unit() {
    install_root_file "$STAGE/daemon/$UNIT_NAME" "$UNIT_DST" 0644
    UNIT_CHANGED=$FILE_CHANGED
    systemctl daemon-reload
    systemctl enable -q "$UNIT_NAME"
    ok "$UNIT_NAME enabled (starts at boot)"
}

# apply_service — start a stopped daemon; restart a running one only for a
# reason (§18.7), because a restart drops a manual hold (§7).
apply_service() {
    local verb reason running hold list=""
    local -a why=()
    if (( NEED_REBOOT )); then
        warn "not starting the daemon: $REBOOT_REASON"
        return 0
    fi
    if systemctl is-active --quiet "$UNIT_NAME"; then
        if (( DAEMON_CHANGED )); then why+=("daemon binary changed"); fi
        if (( UNIT_CHANGED )); then why+=("unit changed"); fi
        # EnvironmentFile= is read only when the daemon starts: a new GUI
        # uid/gid would otherwise leave the socket with the wrong group.
        if (( ENV_CHANGED )); then why+=("daemon.env changed"); fi
        running=$(state_version "$STATE_JSON")
        if [[ -z $running ]]; then
            why+=("the running daemon publishes no fresh state.json")
        elif [[ $running != "$NEW_VERSION" ]]; then
            why+=("the running daemon reports version $running")
        fi
        if (( ${#why[@]} == 0 )); then
            ok "daemon, unit and daemon.env unchanged: left running (an active hold continues)"
            return 0
        fi
        for reason in "${why[@]}"; do list+="${list:+; }$reason"; done
        say "restart needed: $list"
        if hold=$(active_hold "$STATE_JSON"); then
            warn "the active manual hold ($hold) is dropped by this restart; the curve takes over"
        else
            warn "a restart drops any manual fan hold (holds live only in the daemon's memory)"
        fi
        verb=restart
    else
        verb=start
    fi
    SERVICE_TOUCHED=1
    RESTART_EPOCH=$(date +%s.%N)
    systemctl reset-failed "$UNIT_NAME" 2>/dev/null || true
    say "systemctl $verb $UNIT_NAME (Type=notify: returns once the daemon's self-test passed)"
    # Not fatal here: the post-check right after prints the journal and
    # rolls back, which is the §18.8 path.
    if systemctl "$verb" "$UNIT_NAME"; then
        ok "systemctl $verb: done"
    else
        warn "systemctl $verb failed; the post-check decides"
    fi
}

postcheck() {
    local i info="" not_before=0
    if (( NEED_REBOOT )); then
        say "skipped: the daemon starts after the reboot"
        return 0
    fi
    if (( SERVICE_TOUCHED )); then not_before=$RESTART_EPOCH; fi
    for (( i = 0; i < POSTCHECK_SECONDS * 2; i++ )); do
        if systemctl is-active --quiet "$UNIT_NAME" && info=$(state_fresh "$STATE_JSON" "$not_before"); then
            ok "daemon active and publishing: $info"
            return 0
        fi
        sleep 0.5
    done

    trap - ERR
    set +e
    err "post-check failed: within $POSTCHECK_SECONDS s the unit was not active with a fresh $STATE_JSON"
    err "unit: $(systemctl is-active "$UNIT_NAME" 2>/dev/null); state: $(state_fresh "$STATE_JSON" "$not_before" 2>&1)"
    echo "── journalctl -u $UNIT_NAME -n 20 ──"
    journalctl -u "$UNIT_NAME" -n 20 --no-pager 2>&1
    echo "── tail -n 20 $LOG ──"
    tail -n 20 -- "$LOG" 2>/dev/null | tr -d '\000'
    echo
    undo_system_changes
    err "Install rolled back. Fix the cause shown above and run install.sh again."
    exit 1
}

install_desktop_entries() {
    local desktop_dir=$TARGET_HOME/Desktop
    local autostart=$TARGET_HOME/.config/autostart/fan-control.desktop
    local enabled hidden extra="" how
    if ! desktop_path_ok "$SRC"; then
        warn "the checkout path '$SRC' has characters a desktop entry cannot carry unescaped; desktop entries skipped (start the GUI with $SRC/launch.sh)"
        return 0
    fi
    # 0755 on the launcher: Nemo treats a non-executable desktop file as
    # untrusted. A missing ~/Desktop is not created behind the user's back.
    render_desktop_entry "$SRC" > "$WORK/launcher.desktop"
    if [[ -d $desktop_dir ]]; then
        install_user_file "$WORK/launcher.desktop" "$desktop_dir/Fan Control.desktop" 0755
        ok "$desktop_dir/Fan Control.desktop ($USER_FILE_RESULT)"
    else
        say "no $desktop_dir: desktop launcher skipped"
    fi

    # The user's choices are read from a copy taken as the user (see
    # read_user_file); only this copy is parsed as root.
    if read_user_file "$autostart" "$WORK/autostart.old"; then
        read -r enabled hidden < <(autostart_flags "$WORK/autostart.old")
        extra=$(autostart_extra_lines "$WORK/autostart.old")
        how="kept X-GNOME-Autostart-enabled=$enabled, Hidden=$hidden"
    elif (( FIRST_INSTALL )); then
        enabled=true hidden=false
        how="first install: autostart on"
    else
        # Missing on an upgrade means the user removed it. Recreated switched
        # off, so the dashboard's autostart switch (which only rewrites the
        # X-GNOME-Autostart-enabled line of an existing file) still works.
        enabled=false hidden=false
        how="recreated switched OFF: an upgrade never turns autostart on"
    fi
    render_desktop_entry "$SRC" "$enabled" "$hidden" "$extra" > "$WORK/autostart.desktop"
    install_user_file "$WORK/autostart.desktop" "$autostart" 0644
    ok "$autostart ($USER_FILE_RESULT; $how)"
}

notice_running_gui() {
    local pids
    pids=$(gui_pids "$TARGET_USER" "$SRC" | tr '\n' ' ')
    if [[ -n ${pids// /} ]]; then
        warn "Quit and relaunch Fan Control to pick up the new client (running: pid ${pids% })"
    else
        ok "no running Fan Control GUI found"
    fi
}

warn_blanket_sudo() {
    local listing
    listing=$(sudo_listing "$TARGET_USER")
    if has_blanket_nopasswd "$listing"; then
        warn "sudo lets $TARGET_USER run ANY command as root without a password (a 'NOPASSWD: ALL' rule outside this project). While it exists, the narrow fan-control sudo rules protect nothing; see README.md, Troubleshooting."
    fi
    return 0
}

summary() {
    rmdir -- "$BACKUP_DIR" 2>/dev/null || true      # nothing was backed up
    echo
    if (( NEED_REBOOT )); then
        echo "${BOLD}${YELLOW}REBOOT REQUIRED${NC}: $REBOOT_REASON."
        echo "  'options thinkpad_acpi fan_control=1' is configured and the unit is enabled;"
        echo "  the daemon starts on the next boot. Until then the firmware drives the fan."
        echo
    fi
    ok "${BOLD}Done${NC}: thinkpad-fan-controld $NEW_VERSION"
    cat <<EOF
  Launch:         $SRC/launch.sh          (start in the tray: $SRC/launch.sh --tray)
  Browser mode:   python3 $SRC/server.py  -> http://127.0.0.1:7070
  Daemon status:  systemctl status $UNIT_NAME
  Live state:     $STATE_JSON
  Daemon log:     $LOG
  Config:         $CONFIG   (edit it from the dashboard's Fan curve view)
  Environment:    $ENV_FILE
  Uninstall:      sudo $SRC/uninstall.sh [--purge]
EOF
    if [[ -d $BACKUP_DIR ]]; then echo "  Backups:        $BACKUP_DIR"; fi
    echo
}

# run_install — everything after the root check. Offline tests call it in a
# subshell with set_paths pointed at a scratch tree and systemctl stubbed.
run_install() {
    set -Eeuo pipefail
    reset_state
    trap on_err ERR
    trap 'on_signal INT' INT
    trap 'on_signal TERM' TERM
    trap 'on_signal HUP' HUP
    trap cleanup EXIT
    acquire_lock
    echo
    echo "${BOLD}ThinkPad Fan Control — install / upgrade${NC}"

    preflight
    # Two runs within the same second must not share a backup directory.
    if [[ -e $BACKUP_DIR ]]; then BACKUP_DIR=$BACKUP_DIR-$$; fi
    PHASE=system

    step 1 "Backups and the kernel option"
    say "files replaced by this run are backed up to $BACKUP_DIR"
    ensure_modprobe_option
    step 2 "Binaries and alert sound";  install_binaries
    step 3 "daemon.env";                write_daemon_env
    step 4 "Config migration";          migrate_config
    step 5 "Log and logrotate";         install_log_and_logrotate
    step 6 "sudoers";                   install_sudoers
    step 7 "systemd unit";              install_unit; apply_service
    step 8 "Post-check";                postcheck

    PHASE=finishing
    step 9 "Desktop entries";           install_desktop_entries
    step 10 "Running GUI";              notice_running_gui
    warn_blanket_sudo
    PHASE=done
    summary
}

main() {
    parse_args "$@"
    if (( EUID != 0 )); then
        err "run as root: sudo $0"
        exit 1
    fi
    run_install
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
