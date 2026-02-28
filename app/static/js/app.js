/**
 * Uniguard Pro Bridge — Frontend App
 *
 * Single-page app.  No framework; pure vanilla JS.
 * Talks to the FastAPI backend at the same origin.
 */

'use strict';

// ── State ─────────────────────────────────────────────────────────────────────

let cameras = [];
let activeStreamCameraId = null;
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

const get  = (path)         => api('GET',    path);
const post = (path, body)   => api('POST',   path, body);
const del  = (path)         => api('DELETE', path);

// ── Navigation ────────────────────────────────────────────────────────────────

document.querySelectorAll('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(`view-${btn.dataset.view}`).classList.add('active');

    if (btn.dataset.view === 'cameras')   loadCameras();
    if (btn.dataset.view === 'discovery') loadNvrs();
  });
});

// ── Health polling ────────────────────────────────────────────────────────────

async function pollHealth() {
  try {
    const h = await get('/health');
    const dot   = document.getElementById('health-dot');
    const label = document.getElementById('health-label');
    const streams = document.getElementById('active-streams-label');

    dot.className   = `status-dot ${h.ffmpeg_available ? 'ok' : 'error'}`;
    label.textContent = h.ffmpeg_available ? `ffmpeg ready` : `ffmpeg missing`;
    streams.textContent = `${h.active_streams} active stream${h.active_streams !== 1 ? 's' : ''}`;
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
      grid.innerHTML = '<div class="empty-state">No cameras found.<br>Use <strong>Discovery</strong> or <strong>Add Manually</strong>.</div>';
      return;
    }

    // Fetch statuses concurrently
    const statuses = await Promise.all(
      cameras.map(c => get(`/cameras/${c.id}/status`).catch(() => ({ status: 'idle' })))
    );
    const statusMap = Object.fromEntries(cameras.map((c, i) => [c.id, statuses[i]]));
    renderCameraGrid(cameras, statusMap);
  } catch (e) {
    grid.innerHTML = `<div class="empty-state" style="color:var(--danger)">Error loading cameras: ${e.message}</div>`;
  }
}

function renderCameraGrid(cams, statusMap) {
  const grid = document.getElementById('camera-grid');
  grid.innerHTML = '';
  cams.forEach(cam => {
    const s = statusMap[cam.id] || { status: 'idle' };
    grid.appendChild(buildCameraCard(cam, s));
  });
}

function buildCameraCard(cam, status) {
  const card = document.createElement('div');
  card.className = 'camera-card';
  card.id = `cam-card-${cam.id}`;

  const badgeClass = status.status;
  const badgeLabel = status.status.charAt(0).toUpperCase() + status.status.slice(1);

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
      <div class="camera-meta" title="${escHtml(cam.rtsp_url)}">${escHtml(cam.rtsp_url)}</div>
    </div>
    <div class="camera-footer">
      <span class="stream-badge ${badgeClass}" id="badge-${cam.id}">${badgeLabel}</span>
      <div class="camera-actions">
        <button class="btn btn-sm btn-primary" onclick="openStream(${cam.id})">Watch</button>
        <button class="btn btn-sm btn-danger" onclick="deleteCamera(${cam.id}, event)">✕</button>
      </div>
    </div>
  `;
  return card;
}

// ── Stream player ─────────────────────────────────────────────────────────────

async function openStream(cameraId) {
  const cam = cameras.find(c => c.id === cameraId);
  if (!cam) return;

  // Show modal immediately with loading state
  activeStreamCameraId = cameraId;
  document.getElementById('player-title').textContent = cam.name;
  document.getElementById('stream-status-badge').className = 'badge badge-starting';
  document.getElementById('stream-status-badge').textContent = 'Starting…';
  document.getElementById('hls-url-display').textContent = '';
  document.getElementById('overlay-msg').textContent = 'Starting stream…';
  showOverlay(true);
  showModal('player-modal');

  try {
    const result = await post(`/cameras/${cameraId}/start`);
    const hlsUrl = result.hls_url;
    document.getElementById('hls-url-display').textContent = hlsUrl;
    initPlayer(hlsUrl);
    startStatusPoll(cameraId);
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
      enableWorker: false,       // Simpler for local LAN
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
        document.getElementById('overlay-msg').textContent = 'Stream error — check camera/RTSP URL.';
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

function startStatusPoll(cameraId) {
  clearInterval(statusPollTimer);
  statusPollTimer = setInterval(async () => {
    if (!activeStreamCameraId) { clearInterval(statusPollTimer); return; }
    try {
      const s = await get(`/cameras/${cameraId}/status`);
      updateBadge(s.status);
      updateCardBadge(cameraId, s.status);
    } catch { /* ignore */ }
  }, 5_000);
}

async function stopCurrentStream() {
  if (!activeStreamCameraId) return;
  try {
    await post(`/cameras/${activeStreamCameraId}/stop`);
  } catch { /* ignore */ }
  updateCardBadge(activeStreamCameraId, 'idle');
  closePlayer();
}

function stopAndClose() { stopCurrentStream(); }

function closePlayer(event) {
  if (event && event.target !== document.getElementById('player-modal')) return;
  destroyPlayer();
  clearInterval(statusPollTimer);
  activeStreamCameraId = null;
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

function updateCardBadge(cameraId, status) {
  const badge = document.getElementById(`badge-${cameraId}`);
  if (badge) {
    badge.className = `stream-badge ${status}`;
    badge.textContent = status.charAt(0).toUpperCase() + status.slice(1);
  }
}

// ── Delete camera ─────────────────────────────────────────────────────────────

async function deleteCamera(cameraId, event) {
  event.stopPropagation();
  if (!confirm('Remove this camera?')) return;
  try {
    await del(`/cameras/${cameraId}`);
    loadCameras();
  } catch (e) {
    alert(`Error: ${e.message}`);
  }
}

// ── Add camera manually ───────────────────────────────────────────────────────

async function addCameraManual(event) {
  event.preventDefault();
  const name    = document.getElementById('cam-name').value.trim();
  const rtspUrl = document.getElementById('cam-rtsp').value.trim();
  const result  = document.getElementById('add-result');

  result.className = 'alert hidden';
  try {
    await post('/cameras', { name, rtsp_url: rtspUrl });
    result.className = 'alert alert-success';
    result.textContent = `Camera "${name}" added successfully.`;
    result.classList.remove('hidden');
    document.getElementById('add-camera-form').reset();
  } catch (e) {
    result.className = 'alert alert-error';
    result.textContent = `Error: ${e.message}`;
    result.classList.remove('hidden');
  }
}

// ── Discovery view ────────────────────────────────────────────────────────────

async function loadNvrs() {
  const list = document.getElementById('nvr-list');
  list.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    const nvrs = await get('/nvrs');
    if (!nvrs.length) {
      list.innerHTML = '<div class="empty-state">No NVRs registered. Run a scan to find devices.</div>';
      return;
    }
    list.innerHTML = '';
    nvrs.forEach(nvr => list.appendChild(buildNvrCard(nvr)));
  } catch (e) {
    list.innerHTML = `<div class="empty-state" style="color:var(--danger)">Error: ${e.message}</div>`;
  }
}

function buildNvrCard(nvr) {
  const card = document.createElement('div');
  card.className = 'nvr-card';
  const verified = nvr.api_verified
    ? '<span class="stream-badge streaming">Verified</span>'
    : '<span class="stream-badge idle">Unverified</span>';

  card.innerHTML = `
    <div class="nvr-info">
      <div class="nvr-name">${escHtml(nvr.name)} ${verified}</div>
      <div class="nvr-ip">${escHtml(nvr.ip_address)}:${nvr.rtsp_port}</div>
    </div>
    <div class="nvr-actions">
      <button class="btn btn-sm btn-primary" onclick="openImportModal(${nvr.id})">Import Cameras</button>
      <button class="btn btn-sm btn-danger"  onclick="deleteNvr(${nvr.id})">Remove</button>
    </div>
  `;
  return card;
}

async function scanNetwork() {
  const btn     = document.getElementById('scan-btn');
  const results = document.getElementById('scan-results');
  const subnet  = document.getElementById('subnet-input').value.trim() || null;

  btn.textContent = 'Scanning…';
  btn.disabled = true;
  results.innerHTML = '';
  results.classList.remove('hidden');

  try {
    const devices = await post('/discovery/scan', { subnet });
    btn.textContent = 'Scan';
    btn.disabled = false;

    if (!devices.length) {
      results.innerHTML = '<div class="hint">No devices found on port 7441.</div>';
      return;
    }

    results.innerHTML = `<div class="hint">${devices.length} device(s) found:</div>`;
    devices.forEach(d => {
      const item = document.createElement('div');
      item.className = 'scan-result-item';
      item.innerHTML = `
        <div>
          <span class="scan-ip">${escHtml(d.ip)}</span>
          <span class="scan-port"> :${d.port}</span>
        </div>
        <button class="btn btn-sm btn-primary" onclick="openImportModalByIp('${escHtml(d.ip)}')">Import Cameras</button>
      `;
      results.appendChild(item);
    });

    // Refresh NVR list to show newly auto-created entries
    loadNvrs();
  } catch (e) {
    btn.textContent = 'Scan';
    btn.disabled = false;
    results.innerHTML = `<div class="hint" style="color:var(--danger)">Scan failed: ${e.message}</div>`;
  }
}

async function deleteNvr(nvrId) {
  if (!confirm('Remove this NVR and all its cameras?')) return;
  try {
    await del(`/nvrs/${nvrId}`);
    loadNvrs();
  } catch (e) {
    alert(`Error: ${e.message}`);
  }
}

// ── Import modal ──────────────────────────────────────────────────────────────

function openImportModal(nvrId) {
  document.getElementById('import-nvr-id').value = nvrId;
  document.getElementById('import-username').value = '';
  document.getElementById('import-password').value = '';
  document.getElementById('import-result').className = 'alert hidden';
  document.getElementById('import-btn').textContent = 'Import Cameras';
  showModal('import-modal');
}

async function openImportModalByIp(ip) {
  // Find NVR by IP from the current NVR list (already registered by scan)
  try {
    const nvrs = await get('/nvrs');
    const nvr = nvrs.find(n => n.ip_address === ip);
    if (nvr) {
      openImportModal(nvr.id);
    } else {
      alert('NVR not found in database. Try refreshing NVR list.');
    }
  } catch (e) {
    alert(`Error: ${e.message}`);
  }
}

async function submitImport(event) {
  event.preventDefault();
  const nvrId    = document.getElementById('import-nvr-id').value;
  const username = document.getElementById('import-username').value;
  const password = document.getElementById('import-password').value;
  const result   = document.getElementById('import-result');
  const btn      = document.getElementById('import-btn');

  btn.textContent = 'Importing…';
  btn.disabled = true;
  result.className = 'alert hidden';

  try {
    const cams = await post(`/nvrs/${nvrId}/import`, { username, password });
    result.className = 'alert alert-success';
    result.textContent = `Imported ${cams.length} camera(s) successfully.`;
    result.classList.remove('hidden');
    btn.textContent = 'Import Cameras';
    btn.disabled = false;
    // Reload camera list if on cameras view
    loadCameras();
  } catch (e) {
    result.className = 'alert alert-error';
    result.textContent = `Error: ${e.message}`;
    result.classList.remove('hidden');
    btn.textContent = 'Import Cameras';
    btn.disabled = false;
  }
}

function closeImportModal(event) {
  if (event && event.target !== document.getElementById('import-modal')) return;
  hideModal('import-modal');
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

// ── Boot ──────────────────────────────────────────────────────────────────────

loadCameras();
