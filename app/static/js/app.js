/**
 * Uniguard Pro Bridge — Debug Console
 *
 * Single-page app for testing camera streams.
 * Talks to the v2 FastAPI backend at the same origin.
 */

'use strict';

// ── State ─────────────────────────────────────────────────────────────────────

let cameras = [];
let activeStreamCameraId = null;
let activeStreamChannel = null;
let hlsInstance = null;
let statusPollTimer = null;

// ── API helpers ───────────────────────────────────────────────────────────────

async function api(method, path, body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(`/api${path}`, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

const get  = (path)       => api('GET',  path);
const post = (path, body) => api('POST', path, body);

// ── Health polling ────────────────────────────────────────────────────────────

async function pollHealth() {
  try {
    const h = await get('/health');
    const dot     = document.getElementById('health-dot');
    const label   = document.getElementById('health-label');
    const version = document.getElementById('version-label');
    const streams = document.getElementById('active-streams-label');

    dot.className = 'status-dot ok';
    label.textContent = 'Online';
    version.textContent = `v${h.version}`;
    streams.textContent = `${h.active_streams} stream${h.active_streams !== 1 ? 's' : ''}`;
  } catch {
    document.getElementById('health-dot').className = 'status-dot error';
    document.getElementById('health-label').textContent = 'Offline';
  }
}
pollHealth();
setInterval(pollHealth, 15_000);

// ── Cameras view ──────────────────────────────────────────────────────────────

async function loadCameras() {
  const grid = document.getElementById('camera-grid');
  grid.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    cameras = await get('/cameras');
    if (!cameras.length) {
      grid.innerHTML = '<div class="empty-state">No cameras synced from cloud config.<br>Add cameras in the Uniguard Pro web app.</div>';
      return;
    }
    renderCameraGrid(cameras);
  } catch (e) {
    grid.innerHTML = `<div class="empty-state" style="color:var(--danger)">Error loading cameras: ${e.message}</div>`;
  }
}

function renderCameraGrid(cams) {
  const grid = document.getElementById('camera-grid');
  grid.innerHTML = '';
  cams.forEach(cam => grid.appendChild(buildCameraCard(cam)));
}

function buildCameraCard(cam) {
  const card = document.createElement('div');
  card.className = 'camera-card';
  card.id = `cam-card-${cam.id}`;

  const highStatus = cam.streams?.high?.status || 'idle';
  const lowStatus  = cam.streams?.low?.status  || 'idle';

  card.innerHTML = `
    <div class="camera-card-thumb">
      <div class="camera-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path d="M15 10l4.553-2.069A1 1 0 0121 8.868V15.13a1 1 0 01-1.447.9L15 14"/>
          <rect x="2" y="7" width="13" height="10" rx="2"/>
        </svg>
      </div>
    </div>
    <div class="camera-card-body">
      <div class="camera-name" title="${escHtml(cam.name)}">${escHtml(cam.name)}</div>
      <div class="camera-meta" title="${cam.id}">${cam.id}</div>
      ${cam.rtsp_high ? `<div class="camera-rtsp" title="${escHtml(cam.rtsp_high)}"><span class="rtsp-label">HIGH</span> ${escHtml(cam.rtsp_high)}</div>` : ''}
      ${cam.rtsp_low  ? `<div class="camera-rtsp" title="${escHtml(cam.rtsp_low)}"><span class="rtsp-label">LOW</span> ${escHtml(cam.rtsp_low)}</div>` : ''}
    </div>
    <div class="camera-footer">
      <div class="channel-badges">
        <span class="stream-badge ${highStatus}" id="badge-${cam.id}-high">${highStatus === 'streaming' ? 'HIGH \u25cf' : 'HIGH'}</span>
        <span class="stream-badge ${lowStatus}" id="badge-${cam.id}-low">${lowStatus === 'streaming' ? 'LOW \u25cf' : 'LOW'}</span>
      </div>
      <div class="camera-actions">
        ${cam.has_high ? `<button class="btn btn-sm btn-primary" onclick="openStream('${escAttr(cam.id)}','high')">High</button>` : ''}
        ${cam.has_low  ? `<button class="btn btn-sm" onclick="openStream('${escAttr(cam.id)}','low')">Low</button>` : ''}
      </div>
    </div>
  `;
  return card;
}

// ── Stream player ─────────────────────────────────────────────────────────────

async function openStream(cameraId, channel) {
  const cam = cameras.find(c => c.id === cameraId);
  if (!cam) return;

  // Show modal immediately with loading state
  activeStreamCameraId = cameraId;
  activeStreamChannel = channel;
  document.getElementById('player-title').textContent = cam.name;
  document.getElementById('channel-badge').textContent = channel.toUpperCase();
  document.getElementById('stream-status-badge').className = 'badge badge-starting';
  document.getElementById('stream-status-badge').textContent = 'Starting\u2026';
  document.getElementById('hls-url-display').textContent = '';
  document.getElementById('overlay-msg').textContent = 'Starting stream\u2026';
  showOverlay(true);
  showModal('player-modal');

  try {
    const result = await post(`/cameras/${cameraId}/start/${channel}`);
    const hlsUrl = result.hls_url;
    document.getElementById('hls-url-display').textContent = hlsUrl;
    initPlayer(hlsUrl);
    startStatusPoll(cameraId, channel);
  } catch (e) {
    document.getElementById('overlay-msg').textContent = `Failed: ${e.message}`;
    document.getElementById('stream-status-badge').className = 'badge badge-error';
    document.getElementById('stream-status-badge').textContent = 'Error';
  }
}

function initPlayer(hlsUrl) {
  const video = document.getElementById('player-video');
  destroyPlayer();

  if (typeof Hls !== 'undefined' && Hls.isSupported()) {
    hlsInstance = new Hls({
      enableWorker: false,
      lowLatencyMode: false,
      backBufferLength: 10,
    });
    hlsInstance.loadSource(hlsUrl);
    hlsInstance.attachMedia(video);
    hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
      showOverlay(false);
      video.play().catch(() => {});
      updateBadge('streaming');
    });
    hlsInstance.on(Hls.Events.ERROR, (_, data) => {
      if (data.fatal) {
        updateBadge('error');
        document.getElementById('overlay-msg').textContent = 'Stream error \u2014 check camera/RTSP URL.';
        showOverlay(true);
      }
    });
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    // Safari native HLS
    video.src = hlsUrl;
    video.addEventListener('loadeddata', () => {
      showOverlay(false);
      updateBadge('streaming');
    }, { once: true });
    video.play().catch(() => {});
  } else {
    document.getElementById('overlay-msg').textContent = 'HLS not supported in this browser.';
  }
}

function destroyPlayer() {
  if (hlsInstance) {
    hlsInstance.destroy();
    hlsInstance = null;
  }
  const video = document.getElementById('player-video');
  video.pause();
  video.src = '';
}

function startStatusPoll(cameraId, channel) {
  clearInterval(statusPollTimer);
  statusPollTimer = setInterval(async () => {
    if (!activeStreamCameraId) { clearInterval(statusPollTimer); return; }
    try {
      const s = await get(`/cameras/${cameraId}/status`);
      const chStatus = s[channel]?.status || 'idle';
      updateBadge(chStatus);
      updateCardBadge(cameraId, 'high', s.high?.status || 'idle');
      updateCardBadge(cameraId, 'low',  s.low?.status  || 'idle');
    } catch { /* ignore */ }
  }, 5_000);
}

async function stopCurrentStream() {
  if (!activeStreamCameraId) return;
  try {
    await post(`/cameras/${activeStreamCameraId}/stop`);
  } catch { /* ignore */ }
  updateCardBadge(activeStreamCameraId, 'high', 'idle');
  updateCardBadge(activeStreamCameraId, 'low',  'idle');
  closePlayer();
}

function stopAndClose() { stopCurrentStream(); }

function closePlayer(event) {
  if (event && event.target !== document.getElementById('player-modal')) return;
  destroyPlayer();
  clearInterval(statusPollTimer);
  activeStreamCameraId = null;
  activeStreamChannel = null;
  hideModal('player-modal');
}

function showOverlay(show) {
  const overlay = document.getElementById('player-overlay');
  show ? overlay.classList.remove('hidden') : overlay.classList.add('hidden');
}

function updateBadge(status) {
  const badge = document.getElementById('stream-status-badge');
  badge.className = `badge badge-${status}`;
  badge.textContent = status.charAt(0).toUpperCase() + status.slice(1);
}

function updateCardBadge(cameraId, channel, status) {
  const badge = document.getElementById(`badge-${cameraId}-${channel}`);
  if (badge) {
    badge.className = `stream-badge ${status}`;
    badge.textContent = status === 'streaming'
      ? `${channel.toUpperCase()} \u25cf`
      : channel.toUpperCase();
  }
}

// ── Modal helpers ─────────────────────────────────────────────────────────────

function showModal(id) { document.getElementById(id).classList.remove('hidden'); }
function hideModal(id) { document.getElementById(id).classList.add('hidden'); }

// ── Utility ───────────────────────────────────────────────────────────────────

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escAttr(str) {
  return String(str)
    .replace(/'/g, "\\'")
    .replace(/"/g, '&quot;');
}

// ── Boot ──────────────────────────────────────────────────────────────────────

loadCameras();
