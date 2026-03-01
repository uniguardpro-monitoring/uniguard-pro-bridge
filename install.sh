#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Uniguard Pro Bridge — Installer
#
# Supported platforms: Raspberry Pi OS, Ubuntu 22.04+, Debian 11+
#
# One-liner install (picks up ALL prerequisites automatically):
#   curl -sSL https://raw.githubusercontent.com/uniguardpro-monitoring/uniguard-pro-bridge/master/install.sh | sudo bash
#
# If curl isn't installed (e.g. Ubuntu Minimal):
#   wget -qO- https://raw.githubusercontent.com/uniguardpro-monitoring/uniguard-pro-bridge/master/install.sh | sudo bash
#
# Or clone first and run locally:
#   git clone https://github.com/uniguardpro-monitoring/uniguard-pro-bridge.git
#   cd uniguard-pro-bridge && sudo ./install.sh
#
# What this does:
#   1. Checks the platform is Debian/Ubuntu with apt-get
#   2. Installs system dependencies (ffmpeg, python3, python3-venv, git, curl)
#   3. Creates a dedicated 'uniguard' service user
#   4. Clones the repo (or copies from local directory if run from a clone)
#   5. Creates a Python virtual environment and installs packages
#   6. Generates and installs the systemd service
#   7. Starts the service and prints the dashboard URL
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_URL="https://github.com/uniguardpro-monitoring/uniguard-pro-bridge.git"
APP_DIR="/opt/uniguard-bridge"
SERVICE_NAME="uniguard-bridge"
SERVICE_USER="uniguard"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()    { echo -e "${CYAN}[STEP]${NC}  $*"; }

echo ""
echo -e "${CYAN}  ╔══════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}  ║    Uniguard Pro Bridge — Installer           ║${NC}"
echo -e "${CYAN}  ║    RTSP → HLS Streaming Gateway              ║${NC}"
echo -e "${CYAN}  ╚══════════════════════════════════════════════╝${NC}"
echo ""

# ── 1. Require root ──────────────────────────────────────────────────────────

[[ $EUID -ne 0 ]] && error "Please run as root:\n  curl -sSL https://raw.githubusercontent.com/uniguardpro-monitoring/uniguard-pro-bridge/master/install.sh | sudo bash"

# ── 2. Detect & validate platform ────────────────────────────────────────────

step "Checking platform…"

if [ -f /etc/os-release ]; then
    . /etc/os-release
    info "OS: ${PRETTY_NAME:-$ID}"
else
    warn "Could not detect OS — proceeding anyway"
fi

command -v apt-get >/dev/null 2>&1 || error "apt-get not found.\nThis installer requires a Debian/Ubuntu-based system (Raspberry Pi OS, Ubuntu, Debian)."

ARCH=$(uname -m)
info "Architecture: ${ARCH}"

# ── 3. Install system dependencies ───────────────────────────────────────────

step "Installing system packages…"

apt-get update -qq

# curl — needed for future updates via the one-liner
# git  — needed to clone the repo
# ffmpeg — the RTSP→HLS converter
# python3 + python3-venv — application runtime
apt-get install -y -q \
    curl \
    git \
    ffmpeg \
    python3 \
    python3-venv

# Verify critical dependencies installed correctly
ffmpeg -version >/dev/null 2>&1 || error "ffmpeg failed to install"
python3 --version >/dev/null 2>&1 || error "python3 failed to install"
python3 -m venv --help >/dev/null 2>&1 || error "python3-venv failed to install"
git --version >/dev/null 2>&1 || error "git failed to install"

info "ffmpeg:  $(ffmpeg -version 2>&1 | head -1)"
info "python3: $(python3 --version 2>&1)"
info "git:     $(git --version 2>&1)"

# ── 4. Create service user ───────────────────────────────────────────────────

step "Setting up service user…"

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
    info "Created system user: ${SERVICE_USER}"
else
    info "Service user '${SERVICE_USER}' already exists"
fi

# ── 5. Get application code ──────────────────────────────────────────────────

step "Getting application code…"

# Detect if we're running from a local clone or piped via curl/wget
LOCAL_SOURCE=""
if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "" ]]; then
    CANDIDATE="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd 2>/dev/null)" || true
    if [[ -n "$CANDIDATE" && -f "$CANDIDATE/app/main.py" ]]; then
        LOCAL_SOURCE="$CANDIDATE"
    fi
fi

if [[ -n "$LOCAL_SOURCE" ]]; then
    info "Installing from local directory: ${LOCAL_SOURCE}"
    mkdir -p "${APP_DIR}"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
              --exclude=".git" \
              --exclude="__pycache__" \
              --exclude="*.pyc" \
              --exclude=".env" \
              --exclude="hls/" \
              --exclude="uniguard.db" \
              --exclude="venv/" \
              "${LOCAL_SOURCE}/" "${APP_DIR}/"
    else
        find "${APP_DIR}" -mindepth 1 -maxdepth 1 \
             ! -name "hls" ! -name "uniguard.db" ! -name ".env" ! -name "venv" \
             -exec rm -rf {} + 2>/dev/null || true
        cp -a "${LOCAL_SOURCE}/." "${APP_DIR}/"
        rm -rf "${APP_DIR}/.git" "${APP_DIR}/venv" 2>/dev/null || true
        find "${APP_DIR}" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    fi
else
    if [[ -d "${APP_DIR}/.git" ]]; then
        info "Existing installation found — updating from GitHub…"
        cd "${APP_DIR}"
        git fetch origin
        git reset --hard origin/master
    else
        info "Cloning from GitHub…"
        rm -rf "${APP_DIR}"
        git clone --depth 1 "${REPO_URL}" "${APP_DIR}"
    fi
fi

[[ -f "${APP_DIR}/app/main.py" ]] || error "Application code not found at ${APP_DIR}/app/main.py — clone may have failed."

# ── 6. Python virtual environment & dependencies ─────────────────────────────

step "Installing Python dependencies…"

python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

# Verify uvicorn is available
"${APP_DIR}/venv/bin/python" -c "import uvicorn" 2>/dev/null || error "Python dependencies failed to install"
info "Python packages installed successfully"

# ── 7. Directories & permissions ──────────────────────────────────────────────

step "Setting up directories and permissions…"

mkdir -p "${APP_DIR}/hls"
touch "${APP_DIR}/uniguard.db" 2>/dev/null || true

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
info "Ownership set to ${SERVICE_USER}"

# ── 8. Install systemd service ───────────────────────────────────────────────

step "Installing systemd service…"

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
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
systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1
systemctl restart "${SERVICE_NAME}"

# ── 9. Verify everything is running ──────────────────────────────────────────

step "Verifying installation…"

sleep 3
if systemctl is-active --quiet "${SERVICE_NAME}"; then
    info "Service is running"

    # Hit the health endpoint to confirm the API is responding
    if command -v curl >/dev/null 2>&1; then
        HEALTH=$(curl -sf http://127.0.0.1:8080/api/health 2>/dev/null) || HEALTH=""
        if [[ -n "$HEALTH" ]]; then
            info "API is responding — health check passed"
        else
            warn "Service is running but API did not respond yet (may still be starting)"
        fi
    fi
else
    warn "Service may not have started correctly. Check logs:"
    warn "  journalctl -u ${SERVICE_NAME} -n 30"
fi

# ── Done ──────────────────────────────────────────────────────────────────────

LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Uniguard Pro Bridge installed successfully!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  Dashboard:  http://${LOCAL_IP}:8080"
echo "  API docs:   http://${LOCAL_IP}:8080/api/docs"
echo ""
echo "  Manage the service:"
echo "    sudo systemctl status  ${SERVICE_NAME}"
echo "    sudo systemctl restart ${SERVICE_NAME}"
echo "    journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "  Update to latest version:"
echo "    curl -sSL https://raw.githubusercontent.com/uniguardpro-monitoring/uniguard-pro-bridge/master/install.sh | sudo bash"
echo ""
