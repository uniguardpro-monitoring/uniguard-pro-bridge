#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Uniguard Pro Bridge — Uninstaller
#
# Usage:
#   sudo /opt/uniguard-bridge/uninstall.sh
#
# Or remotely:
#   curl -sSL https://raw.githubusercontent.com/uniguardpro-monitoring/uniguard-pro-bridge/master/uninstall.sh | sudo bash
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

APP_DIR="/opt/uniguard-bridge"
SERVICE_NAME="uniguard-bridge"
SERVICE_USER="uniguard"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()    { echo -e "${CYAN}[STEP]${NC}  $*"; }

echo ""
echo -e "${RED}  ╔══════════════════════════════════════════════╗${NC}"
echo -e "${RED}  ║    Uniguard Pro Bridge — Uninstaller          ║${NC}"
echo -e "${RED}  ╚══════════════════════════════════════════════╝${NC}"
echo ""

# ── Require root ─────────────────────────────────────────────────────────────

[[ $EUID -eq 0 ]] || error "Please run as root:  sudo $0"

# ── Confirm ──────────────────────────────────────────────────────────────────

echo -e "${YELLOW}This will completely remove Uniguard Pro Bridge, including:${NC}"
echo "  • Systemd services and timers"
echo "  • Application files at ${APP_DIR}"
echo "  • The '${SERVICE_USER}' system user"
echo "  • Firewall rule for port 8080"
echo ""
read -rp "Are you sure? (y/N) " CONFIRM
[[ "${CONFIRM,,}" == "y" ]] || { echo "Cancelled."; exit 0; }
echo ""

# ── 1. Stop and remove services ─────────────────────────────────────────────

step "Stopping services…"

systemctl disable --now "${SERVICE_NAME}" 2>/dev/null || true
systemctl disable --now "${SERVICE_NAME}-update.timer" 2>/dev/null || true
systemctl stop "${SERVICE_NAME}-update.service" 2>/dev/null || true

rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
rm -f "/etc/systemd/system/${SERVICE_NAME}-update.service"
rm -f "/etc/systemd/system/${SERVICE_NAME}-update.timer"
systemctl daemon-reload
info "Services removed"

# ── 2. Remove application files ─────────────────────────────────────────────

step "Removing application files…"

if [[ -d "${APP_DIR}" ]]; then
    rm -rf "${APP_DIR}"
    info "Removed ${APP_DIR}"
else
    warn "${APP_DIR} not found — already removed?"
fi

# ── 3. Remove service user ──────────────────────────────────────────────────

step "Removing service user…"

if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    userdel "${SERVICE_USER}" 2>/dev/null || true
    info "Removed user '${SERVICE_USER}'"
else
    info "User '${SERVICE_USER}' does not exist — skipping"
fi

# ── 4. Close firewall port ──────────────────────────────────────────────────

step "Cleaning up firewall…"

if command -v ufw >/dev/null 2>&1; then
    UFW_STATUS=$(ufw status 2>/dev/null | head -1) || UFW_STATUS=""
    if echo "$UFW_STATUS" | grep -qi "active"; then
        ufw delete allow 8080/tcp >/dev/null 2>&1 || true
        info "UFW: closed port 8080/tcp"
    fi
elif command -v firewall-cmd >/dev/null 2>&1; then
    if systemctl is-active --quiet firewalld 2>/dev/null; then
        firewall-cmd --permanent --remove-port=8080/tcp >/dev/null 2>&1 || true
        firewall-cmd --reload >/dev/null 2>&1
        info "firewalld: closed port 8080/tcp"
    fi
else
    info "No firewall detected — nothing to clean up"
fi

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Uniguard Pro Bridge has been completely removed.${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  To reinstall:"
echo "    curl -sSL https://raw.githubusercontent.com/uniguardpro-monitoring/uniguard-pro-bridge/master/install.sh | sudo bash"
echo ""
