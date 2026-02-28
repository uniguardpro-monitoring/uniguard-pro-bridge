#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Uniguard Pro Bridge — Raspberry Pi Installer
#
# One-liner install from any internet-connected Pi:
#   curl -sSL https://raw.githubusercontent.com/uniguardpro-monitoring/uniguard-pro-bridge/main/install.sh | sudo bash
#
# Or clone first and run locally:
#   git clone https://github.com/uniguardpro-monitoring/uniguard-pro-bridge.git
#   cd uniguard-pro-bridge && sudo ./install.sh
#
# What this does:
#   1. Installs system dependencies (ffmpeg, python3, python3-venv, git)
#   2. Clones or copies the app to /opt/uniguard-bridge
#   3. Creates a Python virtual environment and installs packages
#   4. Installs and enables the systemd service
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_URL="https://github.com/uniguardpro-monitoring/uniguard-pro-bridge.git"
APP_DIR="/opt/uniguard-bridge"
SERVICE_NAME="uniguard-bridge"
SERVICE_FILE="${SERVICE_NAME}.service"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Require root ──────────────────────────────────────────────────────────────

[[ $EUID -ne 0 ]] && error "Please run as root: sudo ./install.sh"

# ── System dependencies ───────────────────────────────────────────────────────

info "Updating package lists…"
apt-get update -qq

info "Installing system dependencies…"
apt-get install -y -q ffmpeg python3 python3-pip python3-venv git

# Verify ffmpeg
ffmpeg -version >/dev/null 2>&1 || error "ffmpeg installation failed"
info "ffmpeg: $(ffmpeg -version 2>&1 | head -1)"

# ── Get the application code ─────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" 2>/dev/null)" && pwd 2>/dev/null)" || SCRIPT_DIR=""

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/app/main.py" ]]; then
    # Running from a local clone — copy files to APP_DIR
    info "Installing from local directory: ${SCRIPT_DIR}"
    mkdir -p "${APP_DIR}"
    rsync -a --delete \
          --exclude=".git" \
          --exclude="__pycache__" \
          --exclude="*.pyc" \
          --exclude=".env" \
          --exclude="hls/" \
          --exclude="uniguard.db" \
          --exclude="venv/" \
          "${SCRIPT_DIR}/" "${APP_DIR}/"
else
    # Running via curl pipe — clone from GitHub
    info "Cloning from GitHub: ${REPO_URL}"
    if [[ -d "${APP_DIR}/.git" ]]; then
        info "Existing installation found — pulling latest…"
        cd "${APP_DIR}"
        git fetch origin
        git reset --hard origin/main
    else
        rm -rf "${APP_DIR}"
        git clone "${REPO_URL}" "${APP_DIR}"
    fi
fi

# ── Python virtual environment ────────────────────────────────────────────────

info "Setting up Python virtual environment…"
python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

# ── Directories & permissions ─────────────────────────────────────────────────

mkdir -p "${APP_DIR}/hls"
touch "${APP_DIR}/uniguard.db" 2>/dev/null || true

# Set ownership so the service user can write
chown -R www-data:www-data "${APP_DIR}" 2>/dev/null || \
chown -R nobody:nogroup    "${APP_DIR}" 2>/dev/null || true

# ── Systemd service ───────────────────────────────────────────────────────────

info "Installing systemd service…"
cp "${APP_DIR}/${SERVICE_FILE}" "/etc/systemd/system/${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}"; then
    info "Service is running."
else
    warn "Service may not have started correctly. Check logs:"
    warn "  journalctl -u ${SERVICE_NAME} -n 30"
fi

# ── Done ──────────────────────────────────────────────────────────────────────

LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Uniguard Pro Bridge installed successfully!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  Web interface:  http://${LOCAL_IP}:8080"
echo "  API docs:       http://${LOCAL_IP}:8080/api/docs"
echo ""
echo "  Service commands:"
echo "    sudo systemctl status  ${SERVICE_NAME}"
echo "    sudo systemctl restart ${SERVICE_NAME}"
echo "    journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "  To update later:"
echo "    curl -sSL https://raw.githubusercontent.com/uniguardpro-monitoring/uniguard-pro-bridge/main/install.sh | sudo bash"
echo ""
