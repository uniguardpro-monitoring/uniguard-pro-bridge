#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Uniguard Pro Bridge — Installer
#
# Supported platforms: Raspberry Pi OS, Ubuntu 22.04+, Debian 11+
#
# One-liner install:
#   curl -sSL https://raw.githubusercontent.com/uniguardpro-monitoring/uniguard-pro-bridge/master/install.sh | sudo bash
#
# Or clone first and run locally:
#   git clone https://github.com/uniguardpro-monitoring/uniguard-pro-bridge.git
#   cd uniguard-pro-bridge && sudo ./install.sh
#
# What this does:
#   1. Installs system dependencies (ffmpeg, python3, python3-venv, git)
#   2. Creates a dedicated 'uniguard' service user
#   3. Clones or copies the app to /opt/uniguard-bridge
#   4. Creates a Python virtual environment and installs packages
#   5. Installs and enables the systemd service
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_URL="https://github.com/uniguardpro-monitoring/uniguard-pro-bridge.git"
APP_DIR="/opt/uniguard-bridge"
SERVICE_NAME="uniguard-bridge"
SERVICE_FILE="${SERVICE_NAME}.service"
SERVICE_USER="uniguard"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Require root ──────────────────────────────────────────────────────────────

[[ $EUID -ne 0 ]] && error "Please run as root: sudo ./install.sh"

# ── Detect platform ──────────────────────────────────────────────────────────

info "Detecting platform…"
if [ -f /etc/os-release ]; then
    . /etc/os-release
    info "OS: ${PRETTY_NAME:-$ID}"
else
    warn "Could not detect OS — proceeding anyway (requires apt-get)"
fi

command -v apt-get >/dev/null 2>&1 || error "apt-get not found. This installer requires a Debian/Ubuntu-based system."

# ── System dependencies ───────────────────────────────────────────────────────

info "Updating package lists…"
apt-get update -qq

info "Installing system dependencies…"
apt-get install -y -q ffmpeg python3 python3-pip python3-venv git

# Verify ffmpeg
ffmpeg -version >/dev/null 2>&1 || error "ffmpeg installation failed"
info "ffmpeg: $(ffmpeg -version 2>&1 | head -1)"

# Verify python3
python3 --version >/dev/null 2>&1 || error "python3 installation failed"
info "python3: $(python3 --version 2>&1)"

# ── Service user ──────────────────────────────────────────────────────────────

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    info "Creating service user: ${SERVICE_USER}"
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
else
    info "Service user '${SERVICE_USER}' already exists"
fi

# ── Get the application code ─────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" 2>/dev/null)" && pwd 2>/dev/null)" || SCRIPT_DIR=""

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/app/main.py" ]]; then
    # Running from a local clone — copy files to APP_DIR
    info "Installing from local directory: ${SCRIPT_DIR}"
    mkdir -p "${APP_DIR}"
    # Use cp as fallback if rsync is not available
    if command -v rsync >/dev/null 2>&1; then
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
        # Clean target then copy (excluding runtime dirs manually)
        find "${APP_DIR}" -mindepth 1 -maxdepth 1 \
             ! -name "hls" ! -name "uniguard.db" ! -name ".env" ! -name "venv" \
             -exec rm -rf {} + 2>/dev/null || true
        cp -a "${SCRIPT_DIR}/." "${APP_DIR}/"
        rm -rf "${APP_DIR}/.git" "${APP_DIR}/venv" 2>/dev/null || true
        find "${APP_DIR}" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    fi
else
    # Running via curl pipe — clone from GitHub
    info "Cloning from GitHub: ${REPO_URL}"
    if [[ -d "${APP_DIR}/.git" ]]; then
        info "Existing installation found — pulling latest…"
        cd "${APP_DIR}"
        git fetch origin
        git reset --hard origin/master
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

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"

# ── Write systemd service (templated with correct user) ──────────────────────

info "Installing systemd service…"
cat > "/etc/systemd/system/${SERVICE_FILE}" <<UNIT
[Unit]
Description=Uniguard Pro Bridge — RTSP to HLS Streaming Gateway
Documentation=https://github.com/uniguardpro-monitoring/uniguard-pro-bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/uvicorn app.main:app \\
          --host 0.0.0.0 \\
          --port 8080 \\
          --workers 1 \\
          --log-level info

Restart=on-failure
RestartSec=5s
StartLimitBurst=5
StartLimitIntervalSec=60

User=${SERVICE_USER}
Group=${SERVICE_USER}

NoNewPrivileges=true
PrivateTmp=true

Environment="UGBRIDGE_HOST=0.0.0.0"
Environment="UGBRIDGE_PORT=8080"
Environment="UGBRIDGE_STREAM_TIMEOUT_SECONDS=300"

[Install]
WantedBy=multi-user.target
UNIT

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
echo "    curl -sSL https://raw.githubusercontent.com/uniguardpro-monitoring/uniguard-pro-bridge/master/install.sh | sudo bash"
echo ""
