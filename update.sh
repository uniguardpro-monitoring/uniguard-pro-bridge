#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Uniguard Pro Bridge — Auto-Update Script
#
# Called by: uniguard-bridge-update.service (via systemd timer)
# Runs as:  root
# Logs to:  journald (stdout/stderr captured by systemd)
#
# Manual run:  sudo /opt/uniguard-bridge/update.sh
# View logs:   journalctl -u uniguard-bridge-update -n 30
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

APP_DIR="/opt/uniguard-bridge"
SERVICE_NAME="uniguard-bridge"
SERVICE_USER="uniguard"
HEALTH_URL="http://127.0.0.1:8080/api/health"

log()  { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
die()  { log "ERROR: $*" >&2; exit 1; }

# ── Guard: must be root ──────────────────────────────────────────────────────

[[ $EUID -eq 0 ]] || die "Must run as root"

# ── Guard: must be a git repo ────────────────────────────────────────────────

[[ -d "${APP_DIR}/.git" ]] || die "${APP_DIR} is not a git repository — cannot auto-update"

cd "${APP_DIR}"

# ── Step 1: Fetch remote changes ─────────────────────────────────────────────

log "Fetching updates from origin…"
if ! git fetch origin --quiet 2>&1; then
    log "WARN: git fetch failed (network issue?) — skipping update"
    exit 0   # Exit cleanly — transient network failures are not errors
fi

# ── Step 2: Compare local HEAD to remote HEAD ────────────────────────────────

LOCAL_HEAD=$(git rev-parse HEAD)
REMOTE_HEAD=$(git rev-parse origin/master)

if [[ "${LOCAL_HEAD}" == "${REMOTE_HEAD}" ]]; then
    log "Already up to date (${LOCAL_HEAD:0:8})"
    exit 0
fi

log "Update available: ${LOCAL_HEAD:0:8} → ${REMOTE_HEAD:0:8}"

# ── Step 3: Snapshot current state for rollback ──────────────────────────────

ROLLBACK_COMMIT="${LOCAL_HEAD}"

# Check if requirements.txt will change (avoid expensive pip on unchanged deps)
DEPS_CHANGED=false
if ! git diff --quiet "${LOCAL_HEAD}" "${REMOTE_HEAD}" -- requirements.txt 2>/dev/null; then
    DEPS_CHANGED=true
    log "requirements.txt changed — will update Python dependencies"
fi

# ── Step 4: Apply the update ─────────────────────────────────────────────────

log "Applying update…"
git reset --hard origin/master

# ── Step 5: Update Python dependencies if needed ─────────────────────────────

if [[ "${DEPS_CHANGED}" == "true" ]]; then
    log "Installing updated Python dependencies…"
    if ! "${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt" 2>&1; then
        log "ERROR: pip install failed — rolling back"
        git reset --hard "${ROLLBACK_COMMIT}"
        chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
        die "Dependency installation failed; rolled back to ${ROLLBACK_COMMIT:0:8}"
    fi
fi

# ── Step 6: Fix ownership (git reset creates files as root) ──────────────────

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"

# ── Step 7: Restart the service ──────────────────────────────────────────────

log "Restarting ${SERVICE_NAME}…"
systemctl restart "${SERVICE_NAME}"

# ── Step 8: Health check ─────────────────────────────────────────────────────

log "Waiting for service to become healthy…"
HEALTHY=false
for i in $(seq 1 10); do
    sleep 2
    if curl -sf "${HEALTH_URL}" >/dev/null 2>&1; then
        HEALTHY=true
        break
    fi
done

if [[ "${HEALTHY}" == "true" ]]; then
    NEW_VERSION=$(cat "${APP_DIR}/VERSION" 2>/dev/null || echo "unknown")
    log "Update complete — now running version ${NEW_VERSION} (${REMOTE_HEAD:0:8})"
else
    log "WARN: Service did not pass health check after update"
    log "Rolling back to ${ROLLBACK_COMMIT:0:8}…"
    git reset --hard "${ROLLBACK_COMMIT}"
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
    if [[ "${DEPS_CHANGED}" == "true" ]]; then
        "${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt" 2>&1 || true
    fi
    systemctl restart "${SERVICE_NAME}"
    die "Rolled back — health check failed after update"
fi
