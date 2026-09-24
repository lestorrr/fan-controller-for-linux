#!/usr/bin/env bash
# =============================================================================
# ThinkPad Fan Control — installer
#
#   sudo ./install.sh
#
# Installs the daemon, the privileged wrappers, the sudoers rules and the
# systemd unit. Anything it replaces is backed up next to the original with a
# .bak-<timestamp> suffix.
# =============================================================================
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
CONFIG_DIR="/etc/thinkpad-fan-control"
CONFIG="$CONFIG_DIR/config.json"
LOG="/var/log/thinkpad-fan-control.log"
UNIT="thinkpad-fan-control.service"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
CYAN=$'\033[0;36m'; BOLD=$'\033[1m'; NC=$'\033[0m'
log()  { echo "${CYAN}[·]${NC} $*"; }
ok()   { echo "${GREEN}[✓]${NC} $*"; }
warn() { echo "${YELLOW}[!]${NC} $*"; }
die()  { echo "${RED}[✗]${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo $0"

TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"
[[ "$TARGET_USER" != "root" ]] || die "Could not determine the desktop user; run via sudo, not as root directly."

# Everything replaced goes into one timestamped tree rather than leaving
# .bak files scattered next to the originals — the old behaviour dropped a
# stray backup icon right on the user's desktop.
BACKUP_DIR="/var/backups/thinkpad-fan-control/$STAMP"
backup() {
    [[ -e "$1" ]] || return 0
    local dest="$BACKUP_DIR$1"
    install -d "$(dirname "$dest")"
    cp -a "$1" "$dest"
    log "backed up $1"
}

echo
echo "${BOLD}ThinkPad Fan Control installer${NC}  (user: $TARGET_USER)"
echo

# ── 1. thinkpad_acpi fan control ─────────────────────────────────────────────
log "Checking thinkpad_acpi fan control…"
MODPROBE_CONF="/etc/modprobe.d/thinkpad_acpi.conf"
if ! grep -qs "fan_control=1" "$MODPROBE_CONF"; then
    backup "$MODPROBE_CONF"
    echo "options thinkpad_acpi fan_control=1" > "$MODPROBE_CONF"
    ok "wrote $MODPROBE_CONF"
    warn "fan_control was not enabled — a reboot may be needed if the module is in use"
else
    ok "fan_control=1 already configured"
fi
if [[ ! -e /proc/acpi/ibm/fan ]]; then
    warn "/proc/acpi/ibm/fan is missing — is this a ThinkPad with thinkpad_acpi loaded?"
fi

# ── 2. daemon + wrappers ─────────────────────────────────────────────────────
log "Installing daemon and wrappers…"
for f in thinkpad-fan-controld fan-set-level.sh fan-config-save.sh; do
    [[ -f "$SRC/daemon/$f" ]] || die "missing $SRC/daemon/$f"
    backup "/usr/local/bin/$f"
    install -m 0755 -o root -g root "$SRC/daemon/$f" "/usr/local/bin/$f"
done
[[ -f "$SRC/ryzenadj-set-tdp.sh" ]] && \
    install -m 0755 -o root -g root "$SRC/ryzenadj-set-tdp.sh" /usr/local/bin/ryzenadj-set-tdp.sh
ok "installed to /usr/local/bin"

# The old shell daemon is superseded by thinkpad-fan-controld.
if [[ -f /usr/local/bin/thinkpad-fan-control.sh ]]; then
    mv /usr/local/bin/thinkpad-fan-control.sh "/usr/local/bin/thinkpad-fan-control.sh.replaced-$STAMP"
    log "retired the old shell daemon"
fi

# ── 3. config ────────────────────────────────────────────────────────────────
log "Setting up $CONFIG…"
install -d -m 0755 "$CONFIG_DIR"
if [[ -f "$CONFIG" ]]; then
    ok "keeping existing config"
else
    # Let the daemon write its own defaults so there is one source of truth.
    echo '{}' | /usr/local/bin/thinkpad-fan-controld --save-config
    ok "wrote default config"
fi
touch "$LOG"; chmod 0644 "$LOG"

# ── 4. sudoers ───────────────────────────────────────────────────────────────
log "Installing sudoers rules…"
SUDOERS=/etc/sudoers.d/fan-control
backup "$SUDOERS"
sed "s/^jhnlstrlclcn /$TARGET_USER /" "$SRC/daemon/fan-control.sudoers" > "$SUDOERS.new"
chmod 0440 "$SUDOERS.new"
if visudo -cf "$SUDOERS.new" >/dev/null; then
    mv "$SUDOERS.new" "$SUDOERS"
    ok "installed $SUDOERS"
else
    rm -f "$SUDOERS.new"
    die "sudoers file failed validation — nothing was changed"
fi

# ── 5. systemd ───────────────────────────────────────────────────────────────
log "Installing systemd unit…"
backup "/etc/systemd/system/$UNIT"
install -m 0644 -o root -g root "$SRC/daemon/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null 2>&1 || true
if systemctl is-active --quiet "$UNIT"; then
    systemctl restart "$UNIT"
    ok "restarted $UNIT"
else
    systemctl start "$UNIT"
    ok "started $UNIT"
fi

# ── 6. desktop entries ───────────────────────────────────────────────────────
log "Updating desktop entries…"
USER_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
write_desktop() {
    local path="$1" extra="$2"
    backup "$path"
    install -d -m 0755 -o "$TARGET_USER" -g "$TARGET_USER" "$(dirname "$path")"
    cat > "$path" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Fan Control
GenericName=ThinkPad Fan Control
Comment=Monitor and control ThinkPad fan speeds, temperatures and CPU power limits
Exec=python3 $SRC/app.py$extra
Path=$SRC
Icon=$SRC/icon.png
Terminal=false
Categories=System;HardwareSettings;Monitor;
Keywords=fan;temperature;cooling;thinkpad;rpm;tdp;
StartupNotify=true
EOF
    chown "$TARGET_USER:$TARGET_USER" "$path"
    chmod 0755 "$path"
}
write_desktop "$USER_HOME/Desktop/Fan Control.desktop" ""
# Autostart starts in the tray so login doesn't throw a window in your face.
write_desktop "$USER_HOME/.config/autostart/fan-control.desktop" " --tray"
sed -i '/^StartupNotify/a X-GNOME-Autostart-enabled=true\nHidden=false' \
    "$USER_HOME/.config/autostart/fan-control.desktop"
ok "desktop + autostart entries updated"

echo
ok "${BOLD}Done.${NC}"
echo
echo "  Launch:        python3 $SRC/app.py"
echo "  Browser mode:  python3 $SRC/server.py"
echo "  Daemon status: systemctl status $UNIT"
echo "  Daemon log:    tail -f $LOG"
echo "  Config:        $CONFIG  (edit from the GUI's Fan Curve card)"
[[ -d "$BACKUP_DIR" ]] && echo "  Replaced files backed up to $BACKUP_DIR"
echo
