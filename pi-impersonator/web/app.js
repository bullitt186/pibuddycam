/*
 * PiBuddyCam local console.
 *
 * No framework, no CDN, no build step. The module talks to the existing
 * authenticated admin API only:
 *
 *   GET  /api/session        -> { ok, mode, csrf }  (session probe / expiry)
 *   POST /api/login          -> { ok, mode, csrf }  (sets the HttpOnly cookie)
 *   POST /api/logout         -> { ok }              (requires the CSRF header)
 *   GET  /api/dashboard      -> authoritative status/metrics/settings (read)
 *   GET  /api/live/frame     -> latest shared-monitor JPEG (WP-UI5, read)
 *   GET  /api/live/status    -> bounded live-monitor metrics (WP-UI5, read)
 *   WS   /api/live/webrtc        -> local WebRTC signaling (offer/answer/ice
 *                                   JSON frames); no Prusa cloud involved
 *   GET  /api/live/webrtc/status -> bounded local-WebRTC viewer metrics (read)
 *   PATCH /api/settings      -> one coordinator mutation; returns authoritative
 *                               settings on success and rejection (WP-UI3)
 *   GET  /api/integrations   -> redacted Prusa/MQTT config + runtime state
 *   POST /api/mqtt/test      -> non-persistent broker test (existing policy)
 *   PUT  /api/integrations/mqtt  -> re-auth + CSRF atomic MQTT save (WP-UI4)
 *   PUT  /api/integrations/prusa -> re-auth + CSRF atomic Prusa save (WP-UI4)
 *
 * The CSRF token is held in a module-scoped variable (memory only): it is
 * never written to localStorage, sessionStorage, a cookie, or the DOM. Every
 * authenticated request returns the login view on HTTP 401, which is the
 * session-expiry contract. Settings mutations stay pending until the API
 * returns the authoritative coordinator snapshot; the Overview dashboard and
 * the Camera forms converge on that same snapshot.
 */

const SESSION_STATE = {
  csrf: null,
  mode: null,
};

/* Dashboard polling cadence: fast while the tab is visible, slower in the
 * background. A new poll aborts any still-in-flight request. */
const DASHBOARD_INTERVAL_VISIBLE = 5000;
const DASHBOARD_INTERVAL_HIDDEN = 30000;

const dashboard = {
  timer: null,
  controller: null,
  inflight: false,
};

/* Last authoritative dashboard document, used to converge the Camera forms
 * after Prusa/MQTT changes without an extra round-trip. */
let LAST_DASHBOARD = null;

/* MQTT test-before-save state: 'untested' | 'ok' | 'failed'. */
let mqttTestState = 'untested';

/* Signature of the inputs that produced the current successful test. Any edit
 * to an effective tested/saved input invalidates the success so a stale pass
 * can never authorize a save. */
let mqttTestedSignature = null;

/* Redacted configured-state flags from GET /api/integrations. The test body
 * asks the server to use a stored credential only when one is configured, the
 * replacement field is blank, and no clear was requested. */
const MQTT_CONFIGURED = { username: false, password: false };

/* Pending re-auth dialog resolver (true = confirmed, false = cancelled). */
let reauthResolver = null;

const els = {};

function cacheElements() {
  els.boot = document.getElementById('boot');
  els.main = document.getElementById('main');
  els.loginView = document.getElementById('login-view');
  els.appView = document.getElementById('app-view');
  els.loginForm = document.getElementById('login-form');
  els.password = document.getElementById('password');
  els.loginSubmit = document.getElementById('login-submit');
  els.loginError = document.getElementById('login-error');
  els.logout = document.getElementById('logout');
  els.sessionState = document.getElementById('session-state');
  els.navItems = Array.from(document.querySelectorAll('.nav__item'));
  els.panels = Array.from(document.querySelectorAll('[data-view-panel]'));
  els.overviewState = document.getElementById('overview-state');
  els.overviewContent = document.getElementById('overview-content');
  els.overviewFreshness = document.getElementById('overview-freshness');
  els.overviewStatus = document.getElementById('overview-status');
  els.liveState = document.getElementById('live-state');
  els.liveFrame = document.getElementById('live-frame');
  els.livePlaceholder = document.getElementById('live-placeholder');
  els.liveToggle = document.getElementById('live-toggle');
  els.liveDownload = document.getElementById('live-download');
  els.liveDetail = document.getElementById('live-detail');
  els.webrtcState = document.getElementById('live-webrtc-state');
  els.webrtcVideo = document.getElementById('live-webrtc-video');
  els.webrtcPlaceholder = document.getElementById('live-webrtc-placeholder');
  els.webrtcDetail = document.getElementById('live-webrtc-detail');
  els.webrtcConnect = document.getElementById('live-webrtc-connect');
  els.metricResolution = document.getElementById('metric-resolution');
  els.metricQuality = document.getElementById('metric-quality');
  els.metricWifi = document.getElementById('metric-wifi');
  els.metricTemp = document.getElementById('metric-temp');
  els.metricUptime = document.getElementById('metric-uptime');
  els.metricStorage = document.getElementById('metric-storage');
  els.metricVersion = document.getElementById('metric-version');
  els.timelapsesState = document.getElementById('timelapses-state');
  els.timelapseVideoCount = document.getElementById('timelapse-video-count');
  els.timelapseFrameCount = document.getElementById('timelapse-frame-count');
  els.timelapseLibrarySize = document.getElementById('timelapse-library-size');
  els.timelapseBuild = document.getElementById('timelapse-build');
  els.timelapseRefresh = document.getElementById('timelapse-refresh');
  els.timelapseBuildStatus = document.getElementById('timelapse-build-status');
  els.timelapseFilters = document.getElementById('timelapse-filters');
  els.timelapseVideos = document.getElementById('timelapse-videos');
  els.timelapseVideosBody = document.getElementById('timelapse-videos-body');
  els.timelapseVideosEmpty = document.getElementById('timelapse-videos-empty');
  els.timelapseVideosPager = document.getElementById('timelapse-videos-pager');
  els.timelapseVideosPage = document.getElementById('timelapse-videos-page');
  els.timelapseVideosPrev = document.getElementById('timelapse-videos-prev');
  els.timelapseVideosNext = document.getElementById('timelapse-videos-next');
  els.timelapseFramesGrid = document.getElementById('timelapse-frames-grid');
  els.timelapseFramesEmpty = document.getElementById('timelapse-frames-empty');
  els.timelapseFramesPager = document.getElementById('timelapse-frames-pager');
  els.timelapseFramesPage = document.getElementById('timelapse-frames-page');
  els.timelapseFramesPrev = document.getElementById('timelapse-frames-prev');
  els.timelapseFramesNext = document.getElementById('timelapse-frames-next');
  els.settingForms = Array.from(document.querySelectorAll('.setting-form'));
  els.cameraState = document.getElementById('camera-state');
  els.integrationsState = document.getElementById('integrations-state');
  els.prusaForm = document.getElementById('prusa-form');
  els.mqttForm = document.getElementById('mqtt-form');
  els.mqttTest = document.getElementById('mqtt-test');
  els.mqttSaveAnyway = document.getElementById('mqtt-save-anyway');
  els.mqttUsername = document.getElementById('mqtt-username');
  els.mqttPw = document.getElementById('mqtt-password');
  els.mqttClearUsername = document.getElementById('mqtt-clear-username');
  els.mqttClearPw = document.getElementById('mqtt-clear-password');
  els.mqttEffective = document.getElementById('mqtt-effective');
  els.mqttRuntime = document.getElementById('mqtt-runtime');
  els.localOnvif = document.getElementById('local-onvif');
  els.localSnapshot = document.getElementById('local-snapshot');
  els.localHaRtsp = document.getElementById('local-ha-rtsp');
  els.localPrusaRtsp = document.getElementById('local-pibuddycam-rtsp');
  els.copyLocalAccess = document.getElementById('copy-local-access');
  els.systemState = document.getElementById('system-state');
  els.systemAppVersion = document.getElementById('system-app-version');
  els.systemRelease = document.getElementById('system-release');
  els.systemCommit = document.getElementById('system-commit');
  els.systemProvisioning = document.getElementById('system-provisioning');
  els.systemRuntime = document.getElementById('system-runtime');
  els.systemStorage = document.getElementById('system-storage');
  els.systemTemp = document.getElementById('system-temp');
  els.systemUptime = document.getElementById('system-uptime');
  els.systemFreshness = document.getElementById('system-freshness');
  els.systemUpdateChip = document.getElementById('system-update-chip');
  els.systemUpdateInstalled = document.getElementById('system-update-installed');
  els.systemUpdateLatest = document.getElementById('system-update-latest');
  els.systemUpdateChecked = document.getElementById('system-update-checked');
  els.systemUpdateSummary = document.getElementById('system-update-summary');
  els.systemUpdateStatus = document.getElementById('system-update-status');
  els.systemUpdateCheck = document.getElementById('system-update-check');
  els.systemUpdateInstall = document.getElementById('system-update-install');
  els.systemDiagnosticsStatus = document.getElementById('system-diagnostics-status');
  els.systemDiagnosticsOutput = document.getElementById('system-diagnostics-output');
  els.systemDiagnosticsLoad = document.getElementById('system-diagnostics-load');
  els.systemDiagnosticsDownload = document.getElementById('system-diagnostics-download');
  els.systemSshEnabled = document.getElementById('system-ssh-enabled');
  els.systemSshState = document.getElementById('system-ssh-state');
  els.systemSshSave = document.getElementById('system-ssh-save');
  els.systemRecovery = document.getElementById('system-recovery');
  els.systemReboot = document.getElementById('system-reboot');
  els.systemRebootConfirm = document.getElementById('system-reboot-confirm');
  els.systemRebootStatus = document.getElementById('system-reboot-status');
  els.systemReset = document.getElementById('system-reset');
  els.systemResetPhrase = document.getElementById('system-reset-phrase');
  els.systemResetMedia = document.getElementById('system-reset-media');
  els.systemResetStatus = document.getElementById('system-reset-status');
  els.reauthDialog = document.getElementById('reauth-dialog');
  els.reauthForm = document.getElementById('reauth-form');
  els.reauthPassword = document.getElementById('reauth-password');
  els.reauthError = document.getElementById('reauth-error');
  els.reauthConfirm = document.getElementById('reauth-confirm');
  els.reauthCancel = document.getElementById('reauth-cancel');
}

function showBoot() {
  if (els.boot) els.boot.hidden = false;
  if (els.main) els.main.hidden = true;
  if (els.loginView) els.loginView.hidden = true;
  if (els.appView) els.appView.hidden = true;
}

function showLogin(message) {
  stopDashboardPolling();
  stopLiveMonitor();
  disconnectLocalWebrtc();
  stopTimelapsePolling();
  if (updatePollTimer) {
    clearTimeout(updatePollTimer);
    updatePollTimer = null;
  }
  if (els.timelapseBuild) els.timelapseBuild.disabled = false;
  if (els.reauthDialog && els.reauthDialog.open) els.reauthDialog.close();
  // A pending re-auth promise must settle (as cancelled) before the dialog is
  // discarded, or a sensitive action would await forever after an expiry.
  if (reauthResolver) {
    const pending = reauthResolver;
    reauthResolver = null;
    pending(false);
  }
  if (els.boot) els.boot.hidden = true;
  if (els.main) els.main.hidden = false;
  if (els.loginView) els.loginView.hidden = false;
  if (els.appView) els.appView.hidden = true;
  setLoginError(message || '');
  if (els.password) els.password.focus();
}

function showApp(mode) {
  SESSION_STATE.mode = mode || null;
  if (els.boot) els.boot.hidden = true;
  if (els.main) els.main.hidden = false;
  if (els.loginView) els.loginView.hidden = true;
  if (els.appView) els.appView.hidden = false;
  if (els.sessionState) {
    els.sessionState.textContent = mode ? `Signed in · ${mode}` : 'Signed in';
  }
  renderLocalAccess();
  selectView('overview');
  startDashboardPolling();
}

function setLoginError(message) {
  if (!els.loginError) return;
  els.loginError.textContent = message;
  els.loginError.hidden = !message;
}

function setBusy(button, busy, label) {
  if (!button) return;
  if (busy) {
    button.dataset.label = button.textContent;
    button.textContent = label;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
  } else {
    button.textContent = button.dataset.label || label;
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
}

/**
 * Perform one same-origin JSON request. Never throws: a network failure is
 * reported as status 0 so callers can show an honest error state.
 */
async function request(path, options = {}) {
  const headers = { Accept: 'application/json' };
  const init = {
    method: options.method || 'GET',
    credentials: 'same-origin',
    headers,
  };
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  if (options.csrf) {
    headers['X-CSRF-Token'] = SESSION_STATE.csrf || '';
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, { ...init, signal: controller.signal });
    let data = null;
    const text = await response.text();
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_error) {
        data = null;
      }
    }
    return {
      ok: response.ok,
      status: response.status,
      data,
      retryAfter: response.headers.get('Retry-After'),
    };
  } catch (_error) {
    return { ok: false, status: 0, data: null, retryAfter: null };
  } finally {
    clearTimeout(timer);
  }
}

function clearSession() {
  SESSION_STATE.csrf = null;
  SESSION_STATE.mode = null;
}

/** The session-expiry path: any authenticated 401 returns to login. */
function handleExpired() {
  clearSession();
  showLogin('Your session expired. Sign in again.');
}

function selectView(name) {
  els.navItems.forEach((item) => {
    const current = item.dataset.view === name;
    if (current) {
      item.setAttribute('aria-current', 'page');
    } else {
      item.removeAttribute('aria-current');
    }
  });
  els.panels.forEach((panel) => {
    panel.hidden = panel.dataset.viewPanel !== name;
  });
  if (name === 'camera') {
    syncCameraView();
  } else if (name === 'integrations') {
    loadIntegrations();
  } else if (name === 'timelapses') {
    loadTimelapses();
  } else if (name === 'system') {
    loadSystem();
  }
  if (name === 'overview') {
    startLiveMonitor();
  } else {
    stopLiveMonitor();
    disconnectLocalWebrtc();
  }
}

function wireNavigation() {
  els.navItems.forEach((item) => {
    item.addEventListener('click', () => selectView(item.dataset.view));
  });
}

/* ------------------------------------------------------------------ */
/* Overview dashboard (read-only, authenticated)                       */
/* ------------------------------------------------------------------ */

function setText(element, text) {
  if (element) element.textContent = text;
}

function formatBytes(bytes) {
  if (bytes == null || !Number.isFinite(Number(bytes))) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = Number(bytes);
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return '—';
  let total = Math.max(0, Math.floor(Number(seconds)));
  const days = Math.floor(total / 86400);
  total -= days * 86400;
  const hours = Math.floor(total / 3600);
  total -= hours * 3600;
  const minutes = Math.floor(total / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function formatAge(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return 'unknown';
  const value = Math.max(0, Math.floor(Number(seconds)));
  if (value < 60) return `${value}s`;
  if (value < 3600) return `${Math.floor(value / 60)}m`;
  return `${Math.floor(value / 3600)}h`;
}

function setOverviewMessage(kind, text) {
  if (!els.overviewState) return;
  els.overviewState.className = `overview-state overview-state--${kind}`;
  els.overviewState.textContent = text;
  els.overviewState.hidden = false;
}

function clearOverviewMessage() {
  if (!els.overviewState) return;
  els.overviewState.textContent = '';
  els.overviewState.hidden = true;
}

const CHIP_POSITIVE = new Set([
  'running', 'authenticated', 'connected', 'ok', 'available', 'streaming',
  'up-to-date', 'live',
]);
const CHIP_ERROR = new Set([
  'error', 'failed', 'disconnected', 'unauthenticated',
]);

function chipKindFor(state) {
  if (CHIP_POSITIVE.has(state)) return 'chip--ok';
  if (CHIP_ERROR.has(state)) return 'chip--error';
  if (state === 'stopped' || state === 'disabled') return 'chip--muted';
  return 'chip--warn';
}

function renderFreshness(runtime) {
  if (!els.overviewFreshness) return;
  const source = runtime.source || 'unknown';
  const age = runtime.age_seconds;
  let kind = 'chip--muted';
  let text = 'Waiting for data';
  if (source === 'live') {
    kind = 'chip--ok';
    text = 'Live';
  } else if (source === 'stale') {
    kind = 'chip--warn';
    text = age == null ? 'Stale' : `Stale · ${formatAge(age)}`;
  } else if (source === 'unavailable') {
    kind = 'chip--error';
    text = 'Unavailable';
  }
  els.overviewFreshness.className = `chip ${kind}`;
  els.overviewFreshness.textContent = text;
}

function renderMetrics(data) {
  const camera = data.camera || {};
  const resolution = camera.resolution || {};
  setText(
    els.metricResolution,
    resolution.label && resolution.label !== 'unknown' ? resolution.label : '—',
  );
  const quality = camera.quality || {};
  setText(
    els.metricQuality,
    quality.name && quality.name !== 'unknown' ? quality.name : '—',
  );
  const metrics = data.metrics || {};
  setText(
    els.metricWifi,
    metrics.wifi_rssi_dbm == null ? '—' : `${metrics.wifi_rssi_dbm} dBm`,
  );
  setText(
    els.metricTemp,
    metrics.cpu_temperature_c == null
      ? '—'
      : `${Number(metrics.cpu_temperature_c).toFixed(1)} °C`,
  );
  setText(
    els.metricUptime,
    metrics.uptime_seconds == null ? '—' : formatDuration(metrics.uptime_seconds),
  );
  const storage = data.storage || {};
  setText(
    els.metricStorage,
    storage.free_bytes == null ? '—' : formatBytes(storage.free_bytes),
  );
  const version = data.version || {};
  setText(els.metricVersion, version.release || version.application || '—');
}

function statusEntries(data) {
  const camera = data.camera || {};
  const prusa = data.prusa || {};
  const snapshots = data.snapshots || {};
  const rtsp = data.rtsp || {};
  const webrtc = data.webrtc || {};
  const mqtt = data.mqtt || {};
  const storage = data.storage || {};
  const updates = data.updates || {};
  return [
    ['Camera', camera.state],
    ['Prusa', prusa.state],
    ['Snapshots', snapshots.state],
    ['RTSP', rtsp.state],
    ['WebRTC', webrtc.state],
    ['MQTT', mqtt.state],
    ['Storage', storage.state],
    ['Updates', updates.state],
  ];
}

function renderStatusChips(data) {
  if (!els.overviewStatus) return;
  els.overviewStatus.textContent = '';
  statusEntries(data).forEach(([label, state]) => {
    const value = state || 'unknown';
    const item = document.createElement('li');
    item.className = 'status-chips__item';
    const chip = document.createElement('span');
    chip.className = `chip ${chipKindFor(value)}`;
    chip.textContent = `${label}: ${value}`;
    item.appendChild(chip);
    els.overviewStatus.appendChild(item);
  });
}

function renderDashboardLoading() {
  if (els.overviewContent) els.overviewContent.hidden = true;
  setOverviewMessage('info', 'Loading camera status…');
}

function renderDashboardError() {
  if (els.overviewContent) els.overviewContent.hidden = true;
  setOverviewMessage(
    'error',
    'Could not reach the camera status service. Retrying automatically…',
  );
}

function renderDashboard(data) {
  if (els.overviewContent) els.overviewContent.hidden = false;
  LAST_DASHBOARD = data;
  const runtime = data.runtime || {};
  renderFreshness(runtime);
  renderMetrics(data);
  renderStatusChips(data);
  applySettings(data.settings);
  if (runtime.source === 'unavailable') {
    setOverviewMessage(
      'warn',
      'Runtime status is unavailable. The camera process may be starting or restarting.',
    );
  } else if (runtime.source === 'stale' || runtime.fresh === false) {
    const age = runtime.age_seconds;
    setOverviewMessage(
      'warn',
      age == null
        ? 'Showing last known status.'
        : `Showing last known status (${formatAge(age)} old).`,
    );
  } else {
    clearOverviewMessage();
  }
  // Keep the System view's dashboard-derived metrics current without an extra
  // request; the authoritative system/update documents are re-fetched when the
  // view is selected or an action runs.
  if (LAST_SYSTEM) renderSystem(LAST_SYSTEM, LAST_UPDATE || {});
}

function stopDashboardPolling() {
  if (dashboard.timer !== null) {
    clearTimeout(dashboard.timer);
    dashboard.timer = null;
  }
  if (dashboard.controller) {
    dashboard.controller.abort();
    dashboard.controller = null;
  }
  dashboard.inflight = false;
}

function scheduleDashboard() {
  if (dashboard.timer !== null) clearTimeout(dashboard.timer);
  const delay = document.visibilityState === 'visible'
    ? DASHBOARD_INTERVAL_VISIBLE
    : DASHBOARD_INTERVAL_HIDDEN;
  dashboard.timer = setTimeout(pollDashboard, delay);
}

function startDashboardPolling() {
  stopDashboardPolling();
  renderDashboardLoading();
  pollDashboard();
}

async function pollDashboard() {
  if (!SESSION_STATE.csrf || dashboard.inflight) {
    scheduleDashboard();
    return;
  }
  dashboard.inflight = true;
  if (dashboard.controller) dashboard.controller.abort();
  const controller = new AbortController();
  dashboard.controller = controller;
  try {
    const response = await fetch('/api/dashboard', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    });
    if (response.status === 401) {
      handleExpired();
      return;
    }
    let data = null;
    try {
      data = await response.json();
    } catch (_error) {
      data = null;
    }
    if (!response.ok || !data || data.ok !== true) {
      renderDashboardError();
      return;
    }
    renderDashboard(data);
  } catch (error) {
    if (error && error.name === 'AbortError') return;
    renderDashboardError();
  } finally {
    if (dashboard.controller === controller) dashboard.controller = null;
    dashboard.inflight = false;
    if (SESSION_STATE.csrf) scheduleDashboard();
  }
}

/* ------------------------------------------------------------------ */
/* Local monitor (WP-UI5; AC-10/AC-11)                                 */
/*                                                                     */
/* A visibility-aware, authenticated poll of /api/live/frame. The      */
/* server owns the single shared producer; the browser only reads the  */
/* latest frame, so there is no per-viewer decoder and no runaway      */
/* reconnection. Pausing stops polling and lets the viewer lease lapse. */
/* ------------------------------------------------------------------ */

const LIVE_INTERVAL_VISIBLE = 1000;
const LIVE_INTERVAL_HIDDEN = 30000;
const LIVE_MAX_BACKOFF = 30000;
const LIVE_STATUS_INTERVAL = 5000;

const liveMonitor = {
  timer: null,
  controller: null,
  inflight: false,
  paused: false,
  active: false,
  failures: 0,
  objectUrl: null,
  statusAt: 0,
};

function setLiveState(kind, text) {
  if (!els.liveState) return;
  const classes = {
    live: 'chip chip--ok',
    stale: 'chip chip--warn',
    paused: 'chip chip--muted',
    busy: 'chip chip--warn',
    unavailable: 'chip chip--error',
  };
  els.liveState.className = classes[kind] || 'chip chip--muted';
  els.liveState.textContent = text;
}

function showLiveMessage(message) {
  if (els.liveFrame) els.liveFrame.hidden = true;
  if (els.livePlaceholder) {
    els.livePlaceholder.textContent = message;
    els.livePlaceholder.hidden = false;
  }
}

function showLiveFrame(blob, state, age) {
  if (!els.liveFrame) return;
  if (liveMonitor.objectUrl) URL.revokeObjectURL(liveMonitor.objectUrl);
  liveMonitor.objectUrl = URL.createObjectURL(blob);
  els.liveFrame.src = liveMonitor.objectUrl;
  els.liveFrame.hidden = false;
  if (els.livePlaceholder) els.livePlaceholder.hidden = true;
  const ageText = age == null ? '' : ` · ${Number(age).toFixed(1)}s`;
  if (state === 'stale') {
    setLiveState('stale', `Stale snapshot${ageText}`);
  } else {
    setLiveState('live', `Live snapshot${ageText}`);
  }
}

function liveDelay() {
  if (document.visibilityState !== 'visible') return LIVE_INTERVAL_HIDDEN;
  if (liveMonitor.failures <= 0) return LIVE_INTERVAL_VISIBLE;
  const backoff = LIVE_INTERVAL_VISIBLE * Math.pow(2, liveMonitor.failures);
  return Math.min(LIVE_MAX_BACKOFF, backoff);
}

async function refreshLiveStatus() {
  if (!els.liveDetail || !SESSION_STATE.csrf) return;
  const now = Date.now();
  if (liveMonitor.statusAt && now - liveMonitor.statusAt < LIVE_STATUS_INTERVAL) return;
  liveMonitor.statusAt = now;
  try {
    const response = await fetch('/api/live/status', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    });
    if (response.status === 401) {
      handleExpired();
      return;
    }
    if (!response.ok) return;
    const data = await response.json();
    if (!data || data.ok !== true) return;
    if (data.available === false) {
      els.liveDetail.textContent = 'Monitor metrics unavailable.';
      return;
    }
    const producer = data.producer || {};
    const viewers = data.viewers || {};
    const frame = data.frame || {};
    const age = producer.last_frame_age_seconds;
    const ageText = age == null ? '' : ` · frame ${Number(age).toFixed(1)}s`;
    els.liveDetail.textContent =
      `Producer ${producer.running ? 'running' : 'stopped'} · `
      + `${producer.frames_produced || 0} frames · `
      + `${viewers.active || 0}/${viewers.cap || 0} viewers`
      + `${ageText} (${frame.state || 'unknown'})`;
  } catch (_error) {
    /* metrics are best-effort; the frame view is authoritative */
  }
}

function stopLiveMonitor() {
  liveMonitor.active = false;
  if (liveMonitor.timer !== null) {
    clearTimeout(liveMonitor.timer);
    liveMonitor.timer = null;
  }
  if (liveMonitor.controller) {
    liveMonitor.controller.abort();
    liveMonitor.controller = null;
  }
  liveMonitor.inflight = false;
}

function scheduleLive() {
  if (liveMonitor.timer !== null) clearTimeout(liveMonitor.timer);
  if (!liveMonitor.active || liveMonitor.paused) return;
  liveMonitor.timer = setTimeout(pollLive, liveDelay());
}

function startLiveMonitor() {
  liveMonitor.active = true;
  liveMonitor.failures = 0;
  if (liveMonitor.paused) return;
  if (liveMonitor.timer !== null) clearTimeout(liveMonitor.timer);
  pollLive();
}

function syncLiveMonitor() {
  if (!SESSION_STATE.csrf || !liveMonitor.active) {
    stopLiveMonitor();
    return;
  }
  if (document.visibilityState === 'visible') {
    if (liveMonitor.timer !== null) clearTimeout(liveMonitor.timer);
    pollLive();
  } else {
    scheduleLive();
  }
}

async function pollLive() {
  if (!SESSION_STATE.csrf || !liveMonitor.active || liveMonitor.paused) return;
  if (liveMonitor.inflight) {
    scheduleLive();
    return;
  }
  liveMonitor.inflight = true;
  if (liveMonitor.controller) liveMonitor.controller.abort();
  const controller = new AbortController();
  liveMonitor.controller = controller;
  try {
    const response = await fetch('/api/live/frame', {
      credentials: 'same-origin',
      headers: { Accept: 'image/jpeg' },
      signal: controller.signal,
      cache: 'no-store',
    });
    if (response.status === 401) {
      handleExpired();
      return;
    }
    if (response.status === 200) {
      const state = response.headers.get('X-Live-State') || 'live';
      const age = response.headers.get('X-Live-Age');
      const blob = await response.blob();
      if (blob.size > 0) {
        liveMonitor.failures = 0;
        showLiveFrame(blob, state, age);
      } else {
        liveMonitor.failures += 1;
        setLiveState('unavailable', 'No frame');
        showLiveMessage('The local monitor has no frame yet.');
      }
      return;
    }
    if (response.status === 429) {
      liveMonitor.failures += 1;
      setLiveState('busy', 'Too many viewers');
      showLiveMessage('The local monitor is at its viewer limit. Retrying…');
      return;
    }
    const state = response.headers.get('X-Live-State') || 'unavailable';
    liveMonitor.failures += 1;
    if (state === 'stale') {
      setLiveState('stale', 'Stale');
      showLiveMessage('The local monitor frame is stale. Waiting for a fresh frame…');
    } else {
      setLiveState('unavailable', 'Unavailable');
      showLiveMessage('The local monitor is unavailable.');
    }
  } catch (error) {
    if (error && error.name === 'AbortError') return;
    liveMonitor.failures += 1;
    setLiveState('unavailable', 'Unavailable');
    showLiveMessage('Could not reach the local monitor. Retrying…');
  } finally {
    if (liveMonitor.controller === controller) liveMonitor.controller = null;
    liveMonitor.inflight = false;
    refreshLiveStatus();
    if (liveMonitor.active && !liveMonitor.paused) scheduleLive();
  }
}

function toggleLivePause() {
  liveMonitor.paused = !liveMonitor.paused;
  if (els.liveToggle) {
    els.liveToggle.textContent = liveMonitor.paused ? 'Resume' : 'Pause';
  }
  if (liveMonitor.paused) {
    if (liveMonitor.timer !== null) {
      clearTimeout(liveMonitor.timer);
      liveMonitor.timer = null;
    }
    if (liveMonitor.controller) liveMonitor.controller.abort();
    setLiveState('paused', 'Paused');
    showLiveMessage('Local monitor is paused.');
  } else {
    liveMonitor.failures = 0;
    pollLive();
  }
}

async function downloadLiveSnapshot() {
  if (els.liveDownload) els.liveDownload.disabled = true;
  try {
    const response = await fetch('/api/live/frame', {
      credentials: 'same-origin',
      headers: { Accept: 'image/jpeg' },
      cache: 'no-store',
    });
    if (response.status === 401) {
      handleExpired();
      return;
    }
    if (!response.ok) {
      showLiveMessage('No snapshot is available to download right now.');
      return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'pibuddycam-snapshot.jpg';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  } catch (_error) {
    showLiveMessage('Could not download the current snapshot.');
  } finally {
    if (els.liveDownload) els.liveDownload.disabled = false;
  }
}

/* ------------------------------------------------------------------ */
/* Local WebRTC live video                                             */
/*                                                                     */
/* Manual connect/disconnect over a same-origin WebSocket signaling    */
/* channel (GET /api/live/webrtc). Entirely local: no Prusa cloud      */
/* involvement, and limited to one viewer at a time (server-enforced,  */
/* returned as a `viewer_limit` error/ended reason). Unlike the        */
/* snapshot poller above, a failed session is not auto-retried -- a    */
/* stuck GStreamer pipeline is comparatively expensive to keep         */
/* respawning, so the viewer must click Connect again.                 */
/* ------------------------------------------------------------------ */

const localWebrtc = {
  ws: null,
  pc: null,
  connecting: false,
};

function setWebrtcState(kind, text) {
  if (!els.webrtcState) return;
  const classes = {
    connecting: 'chip chip--warn',
    live: 'chip chip--ok',
    idle: 'chip chip--muted',
    error: 'chip chip--error',
  };
  els.webrtcState.className = classes[kind] || 'chip chip--muted';
  els.webrtcState.textContent = text;
}

function showWebrtcMessage(message) {
  if (els.webrtcVideo) els.webrtcVideo.hidden = true;
  if (els.webrtcPlaceholder) {
    els.webrtcPlaceholder.textContent = message;
    els.webrtcPlaceholder.hidden = false;
  }
}

function setWebrtcConnectLabel() {
  if (!els.webrtcConnect) return;
  els.webrtcConnect.disabled = false;
  if (localWebrtc.connecting) {
    els.webrtcConnect.textContent = 'Cancel';
  } else if (localWebrtc.pc || localWebrtc.ws) {
    els.webrtcConnect.textContent = 'Disconnect';
  } else {
    els.webrtcConnect.textContent = 'Connect';
  }
}

function teardownLocalWebrtc() {
  if (localWebrtc.pc) {
    localWebrtc.pc.close();
    localWebrtc.pc = null;
  }
  if (localWebrtc.ws) {
    const ws = localWebrtc.ws;
    localWebrtc.ws = null;
    if (ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify({ type: 'stop' }));
      } catch (_error) {
        /* best-effort; the socket is being closed either way */
      }
    }
    ws.close();
  }
  if (els.webrtcVideo) els.webrtcVideo.srcObject = null;
  localWebrtc.connecting = false;
}

function disconnectLocalWebrtc() {
  if (!localWebrtc.pc && !localWebrtc.ws && !localWebrtc.connecting) return;
  teardownLocalWebrtc();
  setWebrtcState('idle', 'Idle');
  showWebrtcMessage('Live video is not connected.');
  if (els.webrtcDetail) els.webrtcDetail.textContent = '';
  setWebrtcConnectLabel();
}

function webrtcEndedMessage(reason) {
  if (reason === 'viewer_limit') return 'Live video is at its viewer limit.';
  if (reason === 'ice-failed' || reason === 'no-ice-connection' || reason === 'ice-disconnected') {
    return 'Could not establish the video connection.';
  }
  return 'Live video session ended.';
}

function webrtcErrorMessage(code) {
  if (code === 'viewer_limit') return 'Live video is at its viewer limit.';
  return 'Live video signaling error.';
}

function failLocalWebrtc(message) {
  teardownLocalWebrtc();
  setWebrtcState('error', 'Disconnected');
  showWebrtcMessage(message);
  if (els.webrtcDetail) els.webrtcDetail.textContent = message;
  setWebrtcConnectLabel();
}

function connectLocalWebrtc() {
  if (!SESSION_STATE.csrf) return;
  localWebrtc.connecting = true;
  setWebrtcState('connecting', 'Connecting…');
  showWebrtcMessage('Connecting to the camera…');
  if (els.webrtcDetail) els.webrtcDetail.textContent = '';
  setWebrtcConnectLabel();

  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${scheme}//${location.host}/api/live/webrtc`);
  localWebrtc.ws = ws;

  const pc = new RTCPeerConnection();
  localWebrtc.pc = pc;

  pc.ontrack = (event) => {
    if (els.webrtcVideo) {
      els.webrtcVideo.srcObject = event.streams[0];
      els.webrtcVideo.hidden = false;
    }
    if (els.webrtcPlaceholder) els.webrtcPlaceholder.hidden = true;
  };

  pc.onicecandidate = (event) => {
    if (event.candidate && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'ice',
        candidate: event.candidate.candidate,
        sdpMLineIndex: event.candidate.sdpMLineIndex || 0,
      }));
    }
  };

  pc.oniceconnectionstatechange = () => {
    if (localWebrtc.pc !== pc) return;
    const state = pc.iceConnectionState;
    if (state === 'connected' || state === 'completed') {
      localWebrtc.connecting = false;
      setWebrtcState('live', 'Live');
      setWebrtcConnectLabel();
    } else if (state === 'failed' || state === 'closed') {
      failLocalWebrtc('Connection lost.');
    }
  };

  ws.addEventListener('message', async (event) => {
    if (localWebrtc.ws !== ws) return;
    let message = null;
    try {
      message = JSON.parse(event.data);
    } catch (_error) {
      return;
    }
    if (!message || typeof message.type !== 'string') return;
    if (message.type === 'offer') {
      try {
        await pc.setRemoteDescription({ type: 'offer', sdp: message.sdp });
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        ws.send(JSON.stringify({ type: 'answer', sdp: answer.sdp }));
      } catch (_error) {
        failLocalWebrtc('Could not negotiate the video session.');
      }
    } else if (message.type === 'ice') {
      try {
        await pc.addIceCandidate({
          candidate: message.candidate,
          sdpMLineIndex: message.sdpMLineIndex || 0,
        });
      } catch (_error) {
        /* a late/duplicate candidate is not fatal */
      }
    } else if (message.type === 'ended') {
      failLocalWebrtc(webrtcEndedMessage(message.reason));
    } else if (message.type === 'error') {
      failLocalWebrtc(webrtcErrorMessage(message.code));
    }
  });

  ws.addEventListener('close', () => {
    if (localWebrtc.ws === ws) failLocalWebrtc('Signaling connection closed.');
  });

  ws.addEventListener('error', () => {
    if (localWebrtc.ws === ws) {
      failLocalWebrtc('Could not reach the local signaling endpoint.');
    }
  });
}

function toggleLocalWebrtc() {
  if (localWebrtc.pc || localWebrtc.ws || localWebrtc.connecting) {
    disconnectLocalWebrtc();
  } else {
    connectLocalWebrtc();
  }
}

async function probeSession() {
  const result = await request('/api/session');
  if (result.ok && result.data && result.data.ok) {
    SESSION_STATE.csrf = result.data.csrf || null;
    showApp(result.data.mode);
    return;
  }
  showLogin();
}

async function submitLogin(event) {
  event.preventDefault();
  const password = els.password ? els.password.value : '';
  if (!password) {
    setLoginError('Enter the admin password.');
    return;
  }
  setLoginError('');
  setBusy(els.loginSubmit, true, 'Signing in…');
  const result = await request('/api/login', {
    method: 'POST',
    body: { password },
  });
  if (els.password) els.password.value = '';
  setBusy(els.loginSubmit, false, 'Sign in');

  if (result.ok && result.data && result.data.ok) {
    SESSION_STATE.csrf = result.data.csrf || null;
    showApp(result.data.mode);
    return;
  }
  if (result.status === 429) {
    const wait = result.retryAfter ? `${result.retryAfter}s` : 'a minute';
    setLoginError(`Too many attempts. Try again in ${wait}.`);
    return;
  }
  if (result.status === 401) {
    setLoginError('Incorrect password.');
    return;
  }
  setLoginError('Sign-in failed. Check the connection and try again.');
}

async function submitLogout() {
  setBusy(els.logout, true, 'Signing out…');
  const result = await request('/api/logout', { method: 'POST', csrf: true });
  setBusy(els.logout, false, 'Sign out');
  if (result.status === 401) {
    handleExpired();
    return;
  }
  clearSession();
  showLogin();
}

/* ------------------------------------------------------------------ */
/* Timelapse library (WP-UI6; AC-12/AC-13)                             */
/* ------------------------------------------------------------------ */

const TIMELAPSE_PAGE_SIZE = 10;
const TIMELAPSE_FRAME_PAGE_SIZE = 12;
const TIMELAPSE_JOB_POLL_MS = 1500;

const timelapse = {
  page: 1,
  status: 'all',
  pages: 0,
  total: 0,
  bytesTotal: 0,
  framesPage: 1,
  framesPages: 0,
  framesTotal: 0,
  framesBytesTotal: 0,
  controller: null,
  inflight: false,
  jobTimer: null,
  jobId: null,
  loadedOnce: false,
};

function setTimelapseMessage(kind, text) {
  if (!els.timelapsesState) return;
  if (!text) {
    els.timelapsesState.textContent = '';
    els.timelapsesState.hidden = true;
    return;
  }
  els.timelapsesState.className = `overview-state overview-state--${kind}`;
  els.timelapsesState.textContent = text;
  els.timelapsesState.hidden = false;
}

function setTimelapseBuildStatus(text) {
  if (els.timelapseBuildStatus) els.timelapseBuildStatus.textContent = text || '';
}

function formatMediaTimestamp(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return '—';
  const date = new Date(Number(seconds) * 1000);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString();
}

async function loadTimelapses() {
  if (!SESSION_STATE.csrf) return;
  if (!timelapse.loadedOnce) {
    setTimelapseMessage('loading', 'Loading the timelapse library…');
  }
  let ok = false;
  try {
    const [videos, frames] = await Promise.all([
      fetchTimelapseVideos(),
      fetchTimelapseFrames(),
    ]);
    ok = videos && frames;
  } catch (_error) {
    ok = false;
  }
  if (!ok) {
    setTimelapseMessage('error', 'Could not load the timelapse library. Try Refresh.');
    return;
  }
  timelapse.loadedOnce = true;
  setTimelapseMessage('', '');
  updateTimelapseStats();
}

async function fetchTimelapseVideos() {
  if (timelapse.controller) timelapse.controller.abort();
  const controller = new AbortController();
  timelapse.controller = controller;
  const params = new URLSearchParams({
    page: String(timelapse.page),
    page_size: String(TIMELAPSE_PAGE_SIZE),
  });
  if (timelapse.status !== 'all') params.set('status', timelapse.status);
  try {
    const response = await fetch(`/api/media/timelapses?${params.toString()}`, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      signal: controller.signal,
      cache: 'no-store',
    });
    if (response.status === 401) {
      handleExpired();
      return false;
    }
    if (!response.ok) return false;
    const data = await response.json();
    if (!data || data.ok !== true) return false;
    renderTimelapseVideos(data);
    return true;
  } catch (error) {
    if (error && error.name === 'AbortError') return false;
    return false;
  } finally {
    if (timelapse.controller === controller) timelapse.controller = null;
  }
}

async function fetchTimelapseFrames() {
  const params = new URLSearchParams({
    page: String(timelapse.framesPage),
    page_size: String(TIMELAPSE_FRAME_PAGE_SIZE),
  });
  try {
    const response = await fetch(`/api/media/frames?${params.toString()}`, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    });
    if (response.status === 401) {
      handleExpired();
      return false;
    }
    if (!response.ok) return false;
    const data = await response.json();
    if (!data || data.ok !== true) return false;
    renderTimelapseFrames(data);
    return true;
  } catch (_error) {
    return false;
  }
}

function updateTimelapseStats() {
  setText(els.timelapseVideoCount, String(timelapse.total));
  setText(els.timelapseFrameCount, String(timelapse.framesTotal));
  setText(
    els.timelapseLibrarySize,
    formatBytes(timelapse.bytesTotal + timelapse.framesBytesTotal));
}

function renderTimelapseVideos(data) {
  timelapse.total = Number(data.total) || 0;
  timelapse.pages = Number(data.pages) || 0;
  timelapse.bytesTotal = Number(data.bytes_total) || 0;
  const items = Array.isArray(data.items) ? data.items : [];
  const body = els.timelapseVideosBody;
  if (body) body.textContent = '';
  items.forEach((item) => {
    if (!body) return;
    const row = document.createElement('tr');
    const nameCell = document.createElement('td');
    nameCell.textContent = item.name;
    row.appendChild(nameCell);

    const statusCell = document.createElement('td');
    const chip = document.createElement('span');
    chip.className = `chip chip--${statusChipKind(item.status_label)}`;
    chip.textContent = item.status_label || 'unknown';
    statusCell.appendChild(chip);
    row.appendChild(statusCell);

    const sizeCell = document.createElement('td');
    sizeCell.textContent = formatBytes(item.size);
    row.appendChild(sizeCell);

    const createdCell = document.createElement('td');
    createdCell.textContent = formatMediaTimestamp(item.modified);
    row.appendChild(createdCell);

    const playCell = document.createElement('td');
    const play = document.createElement('button');
    play.type = 'button';
    play.className = 'btn btn--small';
    play.textContent = 'Play';
    play.addEventListener('click', () => playTimelapse(item.name));
    playCell.appendChild(play);
    row.appendChild(playCell);

    const downloadCell = document.createElement('td');
    const link = document.createElement('a');
    link.className = 'btn btn--small';
    link.href = item.download_url || `/api/media/timelapses/${encodeURIComponent(item.name)}`;
    link.setAttribute('download', item.name);
    link.textContent = 'Download';
    downloadCell.appendChild(link);
    row.appendChild(downloadCell);
    body.appendChild(row);
  });
  const hasItems = items.length > 0;
  if (els.timelapseVideos) els.timelapseVideos.hidden = !hasItems;
  if (els.timelapseVideosEmpty) els.timelapseVideosEmpty.hidden = hasItems;
  renderPager(
    els.timelapseVideosPager, els.timelapseVideosPage, els.timelapseVideosPrev,
    els.timelapseVideosNext, timelapse.page, timelapse.pages, timelapse.total);
}

function statusChipKind(label) {
  if (label === 'completed') return 'ok';
  if (label === 'error') return 'error';
  if (label === 'pending') return 'warn';
  return 'muted';
}

function renderTimelapseFrames(data) {
  timelapse.framesTotal = Number(data.total) || 0;
  timelapse.framesPages = Number(data.pages) || 0;
  timelapse.framesBytesTotal = Number(data.bytes_total) || 0;
  const items = Array.isArray(data.items) ? data.items : [];
  const grid = els.timelapseFramesGrid;
  if (grid) grid.textContent = '';
  items.forEach((item) => {
    if (!grid) return;
    const figure = document.createElement('figure');
    figure.className = 'media-grid__item';
    const link = document.createElement('a');
    link.href = item.download_url || `/api/media/frames/${encodeURIComponent(item.name)}`;
    link.setAttribute('download', item.name);
    const image = document.createElement('img');
    image.loading = 'lazy';
    image.alt = `Timelapse frame ${item.name}`;
    image.src = item.preview_url || `/api/media/frames/${encodeURIComponent(item.name)}`;
    link.appendChild(image);
    figure.appendChild(link);
    const caption = document.createElement('figcaption');
    caption.textContent = `${item.name} · ${formatBytes(item.size)}`;
    figure.appendChild(caption);
    grid.appendChild(figure);
  });
  const hasItems = items.length > 0;
  if (els.timelapseFramesEmpty) els.timelapseFramesEmpty.hidden = hasItems;
  renderPager(
    els.timelapseFramesPager, els.timelapseFramesPage, els.timelapseFramesPrev,
    els.timelapseFramesNext, timelapse.framesPage, timelapse.framesPages,
    timelapse.framesTotal);
}

function renderPager(pager, label, prev, next, page, pages, total) {
  const show = total > 0 && pages > 1;
  if (pager) pager.hidden = !show;
  if (label) label.textContent = show ? `Page ${page} of ${pages}` : '';
  if (prev) prev.disabled = page <= 1;
  if (next) next.disabled = pages === 0 || page >= pages;
}

function selectTimelapseFilter(status) {
  timelapse.status = status;
  timelapse.page = 1;
  if (els.timelapseFilters) {
    els.timelapseFilters.querySelectorAll('[data-timelapse-filter]').forEach((button) => {
      button.setAttribute(
        'aria-pressed', button.dataset.timelapseFilter === status ? 'true' : 'false');
    });
  }
  fetchTimelapseVideos();
}

function changeTimelapsePage(delta) {
  const next = timelapse.page + delta;
  if (next < 1 || (timelapse.pages && next > timelapse.pages)) return;
  timelapse.page = next;
  fetchTimelapseVideos();
}

function changeTimelapseFramePage(delta) {
  const next = timelapse.framesPage + delta;
  if (next < 1 || (timelapse.framesPages && next > timelapse.framesPages)) return;
  timelapse.framesPage = next;
  fetchTimelapseFrames();
}

function timelapsePlaybackSupported() {
  try {
    const probe = document.createElement('video');
    return Boolean(probe.canPlayType && probe.canPlayType('video/x-msvideo'));
  } catch (_error) {
    return false;
  }
}

function playTimelapse(name) {
  if (!timelapsePlaybackSupported()) {
    setTimelapseBuildStatus(
      'This browser cannot play MJPEG AVI inline. Use Download instead.');
    return;
  }
  window.open(
    `/api/media/timelapses/${encodeURIComponent(name)}`, '_blank', 'noopener');
}

async function buildTimelapse() {
  if (!SESSION_STATE.csrf) return;
  if (els.timelapseBuild) els.timelapseBuild.disabled = true;
  setTimelapseBuildStatus('Queuing a build…');
  const result = await request('/api/media/timelapses/build', {
    method: 'POST', csrf: true, body: {},
  });
  if (result.status === 401) {
    if (els.timelapseBuild) els.timelapseBuild.disabled = false;
    handleExpired();
    return;
  }
  if (result.status === 409) {
    setTimelapseBuildStatus('A build is already running. Waiting for it to finish…');
    if (result.data && result.data.job_id) pollTimelapseBuild(result.data.job_id);
    return;
  }
  if (!result.ok || !result.data || result.data.ok !== true) {
    if (els.timelapseBuild) els.timelapseBuild.disabled = false;
    const reason = result.data && result.data.error
      ? result.data.error : 'The build could not be started.';
    setTimelapseBuildStatus(reason);
    return;
  }
  const job = result.data.job || {};
  setTimelapseBuildStatus(`Build ${job.state || 'pending'} — 0/${job.frames_total || 0} frames`);
  pollTimelapseBuild(job.id);
}

function stopTimelapsePolling() {
  if (timelapse.jobTimer !== null) {
    clearTimeout(timelapse.jobTimer);
    timelapse.jobTimer = null;
  }
}

async function pollTimelapseBuild(jobId) {
  if (!jobId) return;
  timelapse.jobId = jobId;
  stopTimelapsePolling();
  const result = await request(`/api/media/jobs/${encodeURIComponent(jobId)}`);
  if (result.status === 401) {
    handleExpired();
    return;
  }
  if (!result.ok || !result.data || result.data.ok !== true) {
    setTimelapseBuildStatus('The build status is unavailable.');
    if (els.timelapseBuild) els.timelapseBuild.disabled = false;
    return;
  }
  const job = result.data.job || {};
  if (job.state === 'pending' || job.state === 'running') {
    setTimelapseBuildStatus(
      `Building — ${job.frames_written || 0}/${job.frames_total || 0} frames`);
    timelapse.jobTimer = setTimeout(() => pollTimelapseBuild(jobId), TIMELAPSE_JOB_POLL_MS);
    return;
  }
  timelapse.jobId = null;
  if (els.timelapseBuild) els.timelapseBuild.disabled = false;
  if (job.state === 'done') {
    setTimelapseBuildStatus(
      `Build complete — ${job.frames_total || 0} frames assembled.`);
  } else {
    setTimelapseBuildStatus(`Build failed: ${job.reason || 'unknown error'}.`);
  }
  timelapse.page = 1;
  fetchTimelapseVideos();
  fetchTimelapseFrames();
}

function wireTimelapse() {
  if (els.timelapseBuild) els.timelapseBuild.addEventListener('click', buildTimelapse);
  if (els.timelapseRefresh) {
    els.timelapseRefresh.addEventListener('click', () => loadTimelapses());
  }
  if (els.timelapseFilters) {
    els.timelapseFilters.addEventListener('click', (event) => {
      const button = event.target.closest('[data-timelapse-filter]');
      if (button) selectTimelapseFilter(button.dataset.timelapseFilter);
    });
  }
  if (els.timelapseVideosPrev) {
    els.timelapseVideosPrev.addEventListener('click', () => changeTimelapsePage(-1));
  }
  if (els.timelapseVideosNext) {
    els.timelapseVideosNext.addEventListener('click', () => changeTimelapsePage(1));
  }
  if (els.timelapseFramesPrev) {
    els.timelapseFramesPrev.addEventListener('click', () => changeTimelapseFramePage(-1));
  }
  if (els.timelapseFramesNext) {
    els.timelapseFramesNext.addEventListener('click', () => changeTimelapseFramePage(1));
  }
}

/* ------------------------------------------------------------------ */
/* Camera settings (WP-UI3; AC-5/AC-6/AC-7)                            */
/* ------------------------------------------------------------------ */
const QUALITY_NAMES = { 1: 'sd', 2: 'hd', 3: 'fhd' };
const ROTATIONS = [0, 90, 180, 270];

/** Show the 90°/270° cost warning while a transposing rotation is selected. */
function updateRotationWarning() {
  const warning = document.getElementById('rotation-warning');
  if (!warning) return;
  const checked = document.querySelector('input[name="rotation"]:checked');
  warning.hidden = !(checked && (checked.value === '90' || checked.value === '270'));
}

function settingForm(field) {
  return document.querySelector(`.setting-form[data-setting="${field}"]`);
}

function isFormPending(form) {
  return form != null && form.dataset.pending === 'true';
}

function anyFormPending() {
  return els.settingForms.some((form) => isFormPending(form));
}

function setFormBusy(form, busy) {
  if (!form) return;
  form.dataset.pending = busy ? 'true' : 'false';
  const button = form.querySelector('button[type="submit"]');
  if (button) {
    button.disabled = busy;
    if (busy) {
      button.setAttribute('aria-busy', 'true');
    } else {
      button.removeAttribute('aria-busy');
    }
  }
}

function setFormStatus(form, kind, text) {
  if (!form) return;
  const status = form.querySelector('.form-status');
  if (!status) return;
  status.className = `form-status form-status--${kind}`;
  status.textContent = text;
  status.hidden = !text;
}

function clearFormStatus(form) {
  setFormStatus(form, 'info', '');
}

function readSettingValue(form, field) {
  if (field === 'snapshot_upload_enabled' || field === 'timelapse_enabled') {
    const box = form.querySelector('input[type="checkbox"]');
    return box ? box.checked : undefined;
  }
  const radio = form.querySelector('input[type="radio"]:checked');
  if (radio && field === 'rotation') return Number(radio.value);
  if (radio) return radio.value;
  const number = form.querySelector('input[type="number"]');
  if (number) {
    if (number.value === '') return undefined;
    const parsed = Number(number.value);
    if (!Number.isInteger(parsed)) return undefined;
    return parsed;
  }
  const text = form.querySelector('input[type="text"]');
  if (text) return text.value;
  return undefined;
}

function setTextValue(id, value) {
  const input = document.getElementById(id);
  if (input && !input.disabled && document.activeElement !== input
      && value != null) {
    input.value = String(value);
  }
}

function setCheckboxValue(name, checked) {
  const input = document.querySelector(`input[name="${name}"][type="checkbox"]`);
  if (input && document.activeElement !== input && typeof checked === 'boolean') {
    input.checked = checked;
  }
}

function setRadioValue(name, value) {
  if (!value) return;
  const input = document.querySelector(`input[name="${name}"][value="${value}"]`);
  if (input && document.activeElement !== input) input.checked = true;
}

function setNumberValue(id, value) {
  if (value == null || !Number.isFinite(Number(value))) return;
  setTextValue(id, value);
}

/**
 * Apply the authoritative coordinator settings to the Camera forms. Skips
 * focused inputs and any form with an in-flight mutation so polling never
 * clobbers unsaved input. Called from the dashboard poll and after every
 * accepted mutation, so Prusa/MQTT/Web changes converge here too.
 */
function applySettings(settings) {
  if (!settings || typeof settings !== 'object') return;
  if (anyFormPending()) return;
  setTextValue('camera-name', settings.camera_name);
  setRadioValue('quality', QUALITY_NAMES[settings.quality_tier]);
  if (ROTATIONS.includes(settings.rotation)) {
    setRadioValue('rotation', String(settings.rotation));
  }
  updateRotationWarning();
  setCheckboxValue('snapshot_upload_enabled', settings.snapshot_upload_enabled);
  setNumberValue('snapshot-interval', settings.snapshot_interval);
  setCheckboxValue('timelapse_enabled', settings.timelapse_enabled);
  setNumberValue('timelapse-interval', settings.timelapse_interval);
  setNumberValue('timelapse-fps', settings.timelapse_fps);
  if (settings.rtsp_mode === 1 || settings.rtsp_mode === 2) {
    setRadioValue('rtsp_mode', settings.rtsp_mode === 2 ? 'enabled' : 'disabled');
  }
  if (settings.webrtc_mode === 0 || settings.webrtc_mode === 1) {
    setRadioValue('webrtc_mode', settings.webrtc_mode === 1 ? 'enabled' : 'disabled');
  }
}

function syncCameraView() {
  if (LAST_DASHBOARD) applySettings(LAST_DASHBOARD.settings);
}

async function submitSettingForm(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const field = form.dataset.setting;
  // The MQTT and Prusa forms are also `.setting-form` for shared styling but
  // carry no `data-setting`; they own their submit handlers. Without this guard
  // both handlers run and a stray PATCH /api/settings is issued.
  if (!field) return;
  const value = readSettingValue(form, field);
  if (value === undefined) {
    setFormStatus(form, 'error', 'Enter a valid value before saving.');
    return;
  }
  setFormBusy(form, true);
  setFormStatus(form, 'info', 'Applying…');
  const result = await request('/api/settings', {
    method: 'PATCH',
    csrf: true,
    body: { field, value },
  });
  setFormBusy(form, false);
  if (result.status === 401) {
    handleExpired();
    return;
  }
  const data = result.data || null;
  // Converge on the authoritative snapshot even on rejection (AC-6: a TURN
  // quality-lock rejection restores the current value without false success).
  if (data && data.settings) applySettings(data.settings);
  if (result.status === 503) {
    setFormStatus(form, 'error', 'The camera runtime is unavailable. Try again when it is running.');
    return;
  }
  if (result.ok && data && data.ok) {
    setFormStatus(form, 'ok', 'Saved.');
    return;
  }
  const reason = data && data.error ? data.error : 'The change was rejected.';
  setFormStatus(form, 'error', reason);
}

/* ------------------------------------------------------------------ */
/* Integrations (WP-UI4; AC-8/AC-9)                                    */
/* ------------------------------------------------------------------ */

function integrationStatus(form, kind, text) {
  setFormStatus(form, kind, text);
}

function configuredHint(id, configured, label) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = configured ? `${label} configured` : `${label} not configured`;
}

function loadIntegrations() {
  request('/api/integrations').then((result) => {
    if (result.status === 401) {
      handleExpired();
      return;
    }
    if (!result.ok || !result.data || result.data.ok !== true) {
      if (els.integrationsState) {
        els.integrationsState.className = 'overview-state overview-state--error';
        els.integrationsState.textContent = 'Could not load integration settings. Retrying on the next visit.';
        els.integrationsState.hidden = false;
      }
      return;
    }
    if (els.integrationsState) els.integrationsState.hidden = true;
    renderIntegrations(result.data);
  });
}

function renderIntegrations(data) {
  const prusa = data.prusa || {};
  setTextValue('prusa-server', prusa.server);
  configuredHint('prusa-token-state', prusa.token_configured, 'Token');
  configuredHint('prusa-fingerprint-state', prusa.fingerprint_configured, 'Fingerprint');

  const mqtt = data.mqtt || {};
  const enabled = document.getElementById('mqtt-enabled');
  if (enabled && document.activeElement !== enabled) enabled.checked = mqtt.enabled === true;
  setTextValue('mqtt-uri', mqtt.uri);
  setTextValue('mqtt-client-id', mqtt.client_id);
  setTextValue('mqtt-discovery-prefix', mqtt.discovery_prefix);
  setTextValue('mqtt-topic-prefix', mqtt.topic_prefix);
  configuredHint('mqtt-username-state', mqtt.username_configured, 'Username');
  configuredHint('mqtt-password-state', mqtt.password_configured, 'Password');
  configuredHint('mqtt-ca-state', mqtt.ca_file_configured, 'CA file');
  MQTT_CONFIGURED.username = mqtt.username_configured === true;
  MQTT_CONFIGURED.password = mqtt.password_configured === true;
  // An authoritative reload supersedes any earlier test: the values on screen
  // may have changed, so never carry a stale success across it.
  invalidateMqttTest();

  renderEffectiveTopics(mqtt.effective_topics);
  renderMqttRuntime(data.runtime || {});
  renderLocalAccess();
}

function renderEffectiveTopics(topics) {
  if (!els.mqttEffective) return;
  els.mqttEffective.textContent = '';
  if (!topics || typeof topics !== 'object') {
    const item = document.createElement('p');
    item.className = 'field__hint';
    item.textContent = 'Effective topics are unavailable until the device identity is configured.';
    els.mqttEffective.appendChild(item);
    return;
  }
  Object.keys(topics).forEach((name) => {
    const row = document.createElement('div');
    row.className = 'topic-preview__row';
    const term = document.createElement('dt');
    term.textContent = name.replace(/_/g, ' ');
    const value = document.createElement('dd');
    const code = document.createElement('code');
    code.textContent = topics[name];
    value.appendChild(code);
    row.appendChild(term);
    row.appendChild(value);
    els.mqttEffective.appendChild(row);
  });
}

function renderMqttRuntime(runtime) {
  if (!els.mqttRuntime) return;
  const mqtt = runtime.mqtt || {};
  const state = mqtt.state || 'unknown';
  const parts = [`Runtime: ${state}`];
  if (mqtt.configured != null) parts.push(`configured: ${mqtt.configured}`);
  if (runtime.source) parts.push(`status: ${runtime.source}`);
  els.mqttRuntime.textContent = parts.join(' · ');
}

function localHost() {
  return window.location.hostname || 'pibuddycam.local';
}

function renderLocalAccess() {
  const host = localHost();
  setText(els.localOnvif, `http://${host}/onvif/device_service`);
  setText(els.localSnapshot, `http://${host}/snapshot.jpg`);
  setText(els.localHaRtsp, `rtsp://${host}:8555/live`);
  setText(els.localPrusaRtsp, `rtsp://${host}:8554/live`);
}

function collectMqttTestBody() {
  const value = (id) => {
    const input = document.getElementById(id);
    return input ? input.value : '';
  };
  const checked = (id) => {
    const input = document.getElementById(id);
    return !!(input && input.checked);
  };
  // Ask the server for a configured stored credential only when the operator
  // left the replacement blank and did not request a clear. An explicit clear
  // tests that field anonymously; setup mode always uses submitted values.
  const useStoredUsername = MQTT_CONFIGURED.username
    && value('mqtt-username') === ''
    && !checked('mqtt-clear-username');
  const useStoredPassword = MQTT_CONFIGURED.password
    && value('mqtt-password') === ''
    && !checked('mqtt-clear-password');
  return {
    uri: value('mqtt-uri'),
    username: value('mqtt-username'),
    password: value('mqtt-password'),
    ca_file: value('mqtt-ca-file'),
    use_stored_username: useStoredUsername,
    use_stored_password: useStoredPassword,
  };
}

/**
 * Canonical signature of every effective tested/saved MQTT input. A change to
 * the URI, username, password, CA, or either clear flag changes the signature,
 * so a successful test can be tied to exactly the inputs that produced it.
 */
function mqttInputSignature() {
  const value = (id) => {
    const input = document.getElementById(id);
    return input ? input.value : '';
  };
  const checked = (id) => {
    const input = document.getElementById(id);
    return !!(input && input.checked);
  };
  return JSON.stringify({
    uri: value('mqtt-uri'),
    username: value('mqtt-username'),
    password: value('mqtt-password'),
    ca_file: value('mqtt-ca-file'),
    clear_username: checked('mqtt-clear-username'),
    clear_password: checked('mqtt-clear-password'),
  });
}

/**
 * Drop any successful/failed test result and the explicit save-anyway override.
 * Called when a tested input is edited, after a save, and after an integration
 * reload, so a stale success can never authorize a later save.
 */
function invalidateMqttTest(event) {
  // The override checkbox is not itself a tested input; toggling it must not
  // immediately clear the operator's choice.
  if (event && event.target === els.mqttSaveAnyway) return;
  mqttTestState = 'untested';
  mqttTestedSignature = null;
  if (els.mqttSaveAnyway) els.mqttSaveAnyway.checked = false;
}

function collectMqttBody() {
  const enabled = document.getElementById('mqtt-enabled');
  const clearUser = document.getElementById('mqtt-clear-username');
  const clearPass = document.getElementById('mqtt-clear-password');
  const value = (id) => {
    const input = document.getElementById(id);
    return input ? input.value : '';
  };
  return {
    enabled: !!(enabled && enabled.checked),
    uri: value('mqtt-uri'),
    client_id: value('mqtt-client-id'),
    username: value('mqtt-username'),
    password: value('mqtt-password'),
    ca_file: value('mqtt-ca-file'),
    discovery_prefix: value('mqtt-discovery-prefix'),
    topic_prefix: value('mqtt-topic-prefix'),
    clear_username: !!(clearUser && clearUser.checked),
    clear_password: !!(clearPass && clearPass.checked),
  };
}

async function testMqtt() {
  if (!els.mqttForm) return;
  const body = collectMqttTestBody();
  if (!body.uri) {
    integrationStatus(els.mqttForm, 'error', 'Enter the broker URI before testing.');
    return;
  }
  setBusy(els.mqttTest, true, 'Testing…');
  integrationStatus(els.mqttForm, 'info', 'Testing the broker connection…');
  const result = await request('/api/mqtt/test', { method: 'POST', csrf: true, body });
  setBusy(els.mqttTest, false, 'Test connection');
  if (result.status === 401) {
    handleExpired();
    return;
  }
  const data = result.data || {};
  if (result.ok && data.ok) {
    mqttTestState = 'ok';
    mqttTestedSignature = mqttInputSignature();
    integrationStatus(els.mqttForm, 'ok', `Connection test succeeded. ${data.reason || ''}`.trim());
  } else {
    mqttTestState = 'failed';
    mqttTestedSignature = null;
    integrationStatus(
      els.mqttForm,
      'error',
      `Connection test failed: ${data.reason || 'unknown error'}. Check "Save anyway" to store it explicitly.`,
    );
  }
}

async function submitMqtt(event) {
  event.preventDefault();
  if (!els.mqttForm) return;
  // A success only counts for the exact inputs that produced it. This guards
  // against a missed input event: if anything changed, the stale pass is dropped.
  if (mqttTestState === 'ok' && mqttTestedSignature !== mqttInputSignature()) {
    invalidateMqttTest();
  }
  if (mqttTestState !== 'ok' && !(els.mqttSaveAnyway && els.mqttSaveAnyway.checked)) {
    integrationStatus(
      els.mqttForm,
      'error',
      'Test the connection first, or check "Save anyway" to store the configuration without a successful test.',
    );
    return;
  }
  // The route is re-auth gated and its payload carries a broker password, so
  // the admin password can never travel in the save body: confirm first.
  const confirmed = await requestReauth();
  if (!confirmed) {
    integrationStatus(
      els.mqttForm,
      'error',
      'Re-authentication is required to save the MQTT configuration.',
    );
    return;
  }
  const body = collectMqttBody();
  setFormBusy(els.mqttForm, true);
  integrationStatus(els.mqttForm, 'info', 'Saving…');
  const result = await request('/api/integrations/mqtt', {
    method: 'PUT',
    csrf: true,
    body,
  });
  setFormBusy(els.mqttForm, false);
  if (result.status === 401) {
    handleExpired();
    return;
  }
  const data = result.data || {};
  if (result.ok && data.ok) {
    const warning = (data.warnings || [])[0] || 'Saved.';
    integrationStatus(els.mqttForm, 'ok', warning);
    // Drop the typed broker credentials and every test/override flag now that
    // the server holds them; a blank field means "keep the stored value".
    if (els.mqttUsername) els.mqttUsername.value = '';
    if (els.mqttPw) els.mqttPw.value = '';
    if (els.mqttClearUsername) els.mqttClearUsername.checked = false;
    if (els.mqttClearPw) els.mqttClearPw.checked = false;
    invalidateMqttTest();
    loadIntegrations();
    return;
  }
  integrationStatus(els.mqttForm, 'error', data.error || 'Could not save the MQTT configuration.');
}

function collectPrusaBody() {
  const value = (id) => {
    const input = document.getElementById(id);
    return input ? input.value : '';
  };
  const checked = (id) => {
    const input = document.getElementById(id);
    return !!(input && input.checked);
  };
  return {
    server: value('prusa-server'),
    token: value('prusa-token'),
    fingerprint: value('prusa-fingerprint'),
    clear_token: checked('prusa-clear-token'),
    clear_fingerprint: checked('prusa-clear-fingerprint'),
  };
}

async function submitPrusa(event) {
  event.preventDefault();
  if (!els.prusaForm) return;
  const confirmed = await requestReauth();
  if (!confirmed) {
    integrationStatus(els.prusaForm, 'error', 'Re-authentication is required to change credentials.');
    return;
  }
  const body = collectPrusaBody();
  setFormBusy(els.prusaForm, true);
  integrationStatus(els.prusaForm, 'info', 'Saving…');
  const result = await request('/api/integrations/prusa', {
    method: 'PUT',
    csrf: true,
    body,
  });
  setFormBusy(els.prusaForm, false);
  if (result.status === 401) {
    handleExpired();
    return;
  }
  const data = result.data || {};
  if (result.ok && data.ok) {
    const warning = (data.warnings || [])[0] || 'Saved.';
    integrationStatus(els.prusaForm, 'ok', warning);
    const tokenInput = document.getElementById('prusa-token');
    const fingerprint = document.getElementById('prusa-fingerprint');
    if (tokenInput) tokenInput.value = '';
    if (fingerprint) fingerprint.value = '';
    loadIntegrations();
    return;
  }
  integrationStatus(els.prusaForm, 'error', data.error || 'Could not save the Prusa configuration.');
}

async function copyLocalAccess() {
  if (!els.copyLocalAccess) return;
  const lines = [
    `ONVIF: ${els.localOnvif ? els.localOnvif.textContent : ''}`,
    `Snapshot: ${els.localSnapshot ? els.localSnapshot.textContent : ''}`,
    `HA RTSP: ${els.localHaRtsp ? els.localHaRtsp.textContent : ''}`,
    `Prusa RTSP: ${els.localPrusaRtsp ? els.localPrusaRtsp.textContent : ''}`,
  ].join('\n');
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(lines);
    }
    setBusy(els.copyLocalAccess, false, 'Copied');
  } catch (_error) {
    setBusy(els.copyLocalAccess, false, 'Copy failed');
  }
}

/* ------------------------------------------------------------------ */
/* System view (WP-UI7): health, updates, diagnostics, danger zone      */
/*                                                                     */
/* On selecting the view only read-only GETs run (/api/system and      */
/* /api/update); no destructive action fires on load. Sensitive        */
/* actions re-authenticate first and are disabled while in flight.     */
/* ------------------------------------------------------------------ */

let LAST_SYSTEM = null;
let LAST_UPDATE = null;
let LAST_DIAGNOSTICS = '';
let updatePollTimer = null;

function setSystemMessage(kind, text) {
  if (!els.systemState) return;
  els.systemState.className = `overview-state overview-state--${kind}`;
  els.systemState.textContent = text;
  els.systemState.hidden = !text;
}

function updateChipKind(state) {
  if (state === 'up-to-date') return 'chip--ok';
  if (state === 'update-available') return 'chip--warn';
  if (state === 'error' || state === 'invalid') return 'chip--error';
  return 'chip--muted';
}

function formatEpoch(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return '—';
  try {
    return new Date(Number(seconds) * 1000).toLocaleString();
  } catch (_error) {
    return '—';
  }
}

async function loadSystem() {
  if (!SESSION_STATE.csrf) return;
  setSystemMessage('loading', 'Loading system information…');
  const [system, update] = await Promise.all([
    request('/api/system'),
    request('/api/update'),
  ]);
  if (system.status === 401 || update.status === 401) {
    handleExpired();
    return;
  }
  if (!system.ok || !update.ok) {
    setSystemMessage('error', 'Could not load system information.');
  } else {
    setSystemMessage('', '');
  }
  LAST_SYSTEM = system.data || {};
  LAST_UPDATE = update.data || {};
  renderSystem(LAST_SYSTEM, LAST_UPDATE);
}

function renderSystem(system, update) {
  const version = system.version || {};
  setText(els.systemAppVersion, version.application || '—');
  setText(els.systemRelease, version.release || '—');
  setText(els.systemCommit, version.source_commit || '—');
  const provisioning = system.provisioning || {};
  setText(
    els.systemProvisioning,
    provisioning.state
      ? `${provisioning.state} (${provisioning.source || 'unknown'})`
      : '—',
  );
  const ssh = system.ssh || {};
  if (els.systemSshEnabled) els.systemSshEnabled.checked = ssh.enabled === true;
  setText(
    els.systemSshState,
    ssh.ok
      ? (ssh.enabled ? 'SSH is enabled' : 'SSH is disabled')
      : 'SSH state unknown',
  );

  const dash = LAST_DASHBOARD || {};
  const runtime = dash.runtime || {};
  setText(els.systemRuntime, runtime.source || '—');
  const metrics = dash.metrics || {};
  setText(
    els.systemTemp,
    metrics.cpu_temperature_c == null
      ? '—'
      : `${Number(metrics.cpu_temperature_c).toFixed(1)} °C`,
  );
  setText(
    els.systemUptime,
    metrics.uptime_seconds == null ? '—' : formatDuration(metrics.uptime_seconds),
  );
  const storage = dash.storage || {};
  setText(
    els.systemStorage,
    storage.free_bytes == null ? '—' : formatBytes(storage.free_bytes),
  );
  setText(
    els.systemFreshness,
    runtime.fresh
      ? `Runtime ${runtime.source || 'live'} · data current`
      : `Runtime ${runtime.source || 'unknown'} · data may be stale`,
  );
  renderUpdate(update);
}

function renderUpdate(update) {
  const state = update.state || 'unknown';
  if (els.systemUpdateChip) {
    els.systemUpdateChip.className = `chip ${updateChipKind(state)}`;
    els.systemUpdateChip.textContent = state.replace(/-/g, ' ');
  }
  setText(els.systemUpdateInstalled, update.installed_version || '—');
  setText(els.systemUpdateLatest, update.latest_version || '—');
  setText(els.systemUpdateChecked, formatEpoch(update.last_check));
  setText(els.systemUpdateSummary, update.release_summary || '');
  if (els.systemUpdateInstall) {
    els.systemUpdateInstall.disabled = !(
      update.available === true
      && state === 'update-available'
      && !update.installing
    );
  }
  if (!update.available) {
    setText(els.systemUpdateStatus, update.reason || 'Update control unavailable.');
  } else if (update.installing) {
    setText(
      els.systemUpdateStatus,
      'Installing… the console may disconnect. Reconnect and re-check the state.',
    );
  } else if (update.checking) {
    setText(els.systemUpdateStatus, 'Checking for updates…');
  } else if (update.check_reason) {
    setText(els.systemUpdateStatus, `Last check failed: ${update.check_reason}`);
  } else if (update.install_reason) {
    setText(els.systemUpdateStatus, `Last install failed: ${update.install_reason}`);
  } else if (update.source_configured === false) {
    setText(els.systemUpdateStatus, 'No update source is configured on this device.');
  } else {
    setText(els.systemUpdateStatus, '');
  }
}

async function checkForUpdate() {
  if (!els.systemUpdateCheck) return;
  setBusy(els.systemUpdateCheck, true, 'Checking…');
  const result = await request('/api/update/check', {
    method: 'POST', csrf: true, body: {},
  });
  setBusy(els.systemUpdateCheck, false, 'Check for updates');
  if (result.status === 401) {
    handleExpired();
    return;
  }
  if (result.status === 409) {
    setText(els.systemUpdateStatus, 'A check is already in progress.');
  } else if (result.status === 503) {
    setText(els.systemUpdateStatus, 'Update control unavailable.');
  } else if (result.ok) {
    setText(els.systemUpdateStatus, 'Report-only check started. This never installs.');
  } else {
    setText(
      els.systemUpdateStatus,
      (result.data && result.data.error) || 'Check failed.',
    );
  }
  scheduleUpdatePoll();
}

async function installUpdate() {
  if (!els.systemUpdateInstall) return;
  const confirmed = await requestReauth();
  if (!confirmed) {
    setText(els.systemUpdateStatus, 'Re-authentication is required to install.');
    return;
  }
  setBusy(els.systemUpdateInstall, true, 'Installing…');
  const result = await request('/api/update/install', {
    method: 'POST', csrf: true, body: {},
  });
  if (result.status === 401) {
    handleExpired();
    return;
  }
  if (result.ok) {
    setText(
      els.systemUpdateStatus,
      (result.data && result.data.warning)
        || 'Install started. The console may disconnect.',
    );
  } else if (result.status === 0) {
    setText(
      els.systemUpdateStatus,
      'The console lost connection. The install may have started; reconnect and check the update state.',
    );
  } else if (result.status === 409) {
    setText(els.systemUpdateStatus, 'An install is already in progress.');
  } else {
    setText(
      els.systemUpdateStatus,
      (result.data && result.data.error) || 'Install failed.',
    );
  }
  setBusy(els.systemUpdateInstall, false, 'Install update');
  renderUpdate({ ...(LAST_UPDATE || {}), installing: result.ok || result.status === 0 });
  scheduleUpdatePoll();
}

function scheduleUpdatePoll() {
  if (updatePollTimer) clearTimeout(updatePollTimer);
  updatePollTimer = setTimeout(async () => {
    if (!SESSION_STATE.csrf) return;
    const result = await request('/api/update');
    if (result.status === 401) {
      handleExpired();
      return;
    }
    if (result.ok) {
      LAST_UPDATE = result.data || {};
      renderUpdate(LAST_UPDATE);
      if (LAST_UPDATE.installing || LAST_UPDATE.checking) scheduleUpdatePoll();
    }
  }, 3000);
}

async function loadDiagnostics() {
  if (!els.systemDiagnosticsLoad) return;
  setBusy(els.systemDiagnosticsLoad, true, 'Loading…');
  const result = await request('/api/diagnostics');
  setBusy(els.systemDiagnosticsLoad, false, 'Load diagnostics');
  if (result.status === 401) {
    handleExpired();
    return;
  }
  const data = result.data || {};
  if (!result.ok || !data.available) {
    setText(els.systemDiagnosticsStatus, data.reason || 'Diagnostics unavailable.');
    if (els.systemDiagnosticsOutput) els.systemDiagnosticsOutput.hidden = true;
    if (els.systemDiagnosticsDownload) els.systemDiagnosticsDownload.disabled = true;
    return;
  }
  LAST_DIAGNOSTICS = data.text || '';
  if (els.systemDiagnosticsOutput) {
    els.systemDiagnosticsOutput.textContent = LAST_DIAGNOSTICS;
    els.systemDiagnosticsOutput.hidden = false;
  }
  if (els.systemDiagnosticsDownload) {
    els.systemDiagnosticsDownload.disabled = !LAST_DIAGNOSTICS;
  }
  const stale = data.fresh === false ? ' · stale' : '';
  setText(
    els.systemDiagnosticsStatus,
    `Bounded diagnostics · ${data.lines || 0} lines`
      + `${data.truncated ? ' · truncated' : ''}${stale}`,
  );
}

function downloadDiagnostics() {
  if (!LAST_DIAGNOSTICS) return;
  const blob = new Blob([LAST_DIAGNOSTICS], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'pibuddycam-diagnostics.txt';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

async function saveSsh() {
  if (!els.systemSshSave) return;
  const enabled = !!(els.systemSshEnabled && els.systemSshEnabled.checked);
  const confirmed = await requestReauth();
  if (!confirmed) {
    setText(els.systemSshState, 'Re-authentication is required to change SSH.');
    return;
  }
  setBusy(els.systemSshSave, true, 'Saving…');
  const result = await request('/api/ssh', {
    method: 'POST', csrf: true, body: { enabled },
  });
  setBusy(els.systemSshSave, false, 'Save SSH');
  if (result.status === 401) {
    handleExpired();
    return;
  }
  const data = result.data || {};
  if (result.ok && data.ok) {
    setText(els.systemSshState, data.enabled ? 'SSH is enabled' : 'SSH is disabled');
  } else {
    setText(els.systemSshState, data.reason || data.error || 'Could not change SSH.');
  }
}

async function enterRecovery() {
  if (!window.confirm(
    'Enter setup / recovery mode? The console disconnects and the device '
    + 'restarts into the setup hotspot.')) {
    return;
  }
  const confirmed = await requestReauth();
  if (!confirmed) return;
  const result = await request('/api/recovery/enter-setup', {
    method: 'POST',
    csrf: true,
    body: { reason: 'operator requested from System view' },
  });
  if (result.status === 401) {
    handleExpired();
    return;
  }
  const data = result.data || {};
  if (result.ok && data.ok) {
    setText(
      els.systemSshState,
      'Recovery sentinel written. The device enters setup on the next boot.',
    );
  } else {
    setText(els.systemSshState, data.reason || data.error || 'Could not enter recovery.');
  }
}

async function rebootDevice() {
  if (!els.systemReboot) return;
  if (!(els.systemRebootConfirm && els.systemRebootConfirm.checked)) {
    setText(els.systemRebootStatus, 'Check the acknowledgement first.');
    return;
  }
  const confirmed = await requestReauth();
  if (!confirmed) return;
  setBusy(els.systemReboot, true, 'Rebooting…');
  const result = await request('/api/reboot', {
    method: 'POST', csrf: true, body: { confirm: true },
  });
  setBusy(els.systemReboot, false, 'Reboot camera');
  if (result.status === 401) {
    handleExpired();
    return;
  }
  if (result.status === 429) {
    setText(
      els.systemRebootStatus,
      `Reboot is rate-limited. Try again in ${result.retryAfter || 'a minute'}s.`,
    );
  } else if (result.ok) {
    setText(
      els.systemRebootStatus,
      'Reboot accepted. The console disconnects until the camera returns.',
    );
  } else if (result.status === 0) {
    setText(
      els.systemRebootStatus,
      'The console lost connection. The camera may be rebooting.',
    );
  } else {
    setText(
      els.systemRebootStatus,
      (result.data && result.data.error) || 'Reboot failed.',
    );
  }
}

function resetPhraseReady() {
  const value = els.systemResetPhrase
    ? els.systemResetPhrase.value.trim().toUpperCase()
    : '';
  if (els.systemReset) els.systemReset.disabled = value !== 'RESET';
}

function resetStepOk(result) {
  if (result.status === 401) {
    handleExpired();
    return false;
  }
  if (!result.ok) {
    setBusy(els.systemReset, false, 'Factory reset');
    setText(
      els.systemResetStatus,
      (result.data && result.data.error) || 'Factory reset step failed.',
    );
    return false;
  }
  return true;
}

async function factoryReset() {
  if (!els.systemReset) return;
  const phrase = els.systemResetPhrase
    ? els.systemResetPhrase.value.trim().toUpperCase()
    : '';
  if (phrase !== 'RESET') {
    setText(els.systemResetStatus, 'Type RESET to confirm.');
    return;
  }
  const includeMedia = !!(els.systemResetMedia && els.systemResetMedia.checked);
  const warning = includeMedia
    ? 'Factory reset deletes durable configuration and stored timelapse media. Continue?'
    : 'Factory reset deletes durable configuration and keeps timelapse media. Continue?';
  if (!window.confirm(warning)) return;
  const confirmed = await requestReauth();
  if (!confirmed) {
    setText(els.systemResetStatus, 'Re-authentication is required.');
    return;
  }
  setBusy(els.systemReset, true, 'Resetting…');
  setText(els.systemResetStatus, 'Starting reset…');
  const begin = await request('/api/reset/begin', {
    method: 'POST', csrf: true, body: {},
  });
  if (!resetStepOk(begin)) return;
  const confirm = await request('/api/reset/confirm', {
    method: 'POST', csrf: true, body: {},
  });
  if (!resetStepOk(confirm)) return;
  const execute = await request('/api/reset/execute', {
    method: 'POST', csrf: true, body: { include_timelapse: includeMedia },
  });
  setBusy(els.systemReset, false, 'Factory reset');
  if (execute.status === 401) {
    handleExpired();
    return;
  }
  const data = execute.data || {};
  if (execute.ok && data.ok) {
    const report = data.report || {};
    setText(
      els.systemResetStatus,
      `Reset complete. ${report.file_count || 0} files removed; a dated backup was retained.`,
    );
  } else {
    setText(els.systemResetStatus, data.error || 'Factory reset failed.');
  }
}

/* ------------------------------------------------------------------ */
/* Re-authentication dialog (reused by credential saves)               */
/* ------------------------------------------------------------------ */

function setReauthError(message) {
  if (!els.reauthError) return;
  els.reauthError.textContent = message;
  els.reauthError.hidden = !message;
}

function requestReauth() {
  return new Promise((resolve) => {
    reauthResolver = resolve;
    if (els.reauthPassword) els.reauthPassword.value = '';
    setReauthError('');
    if (els.reauthDialog && typeof els.reauthDialog.showModal === 'function') {
      els.reauthDialog.showModal();
      if (els.reauthPassword) els.reauthPassword.focus();
      return;
    }
    setReauthError('This browser cannot show the confirmation dialog.');
    reauthResolver = null;
    resolve(false);
  });
}

function resolveReauth(confirmed) {
  if (els.reauthDialog && els.reauthDialog.open) els.reauthDialog.close();
  const resolver = reauthResolver;
  reauthResolver = null;
  if (resolver) resolver(confirmed);
}

async function submitReauth(event) {
  event.preventDefault();
  const password = els.reauthPassword ? els.reauthPassword.value : '';
  if (!password) {
    setReauthError('Enter your admin password.');
    return;
  }
  setBusy(els.reauthConfirm, true, 'Confirming…');
  const result = await request('/api/reauth', { method: 'POST', csrf: true, body: { password } });
  setBusy(els.reauthConfirm, false, 'Confirm');
  if (els.reauthPassword) els.reauthPassword.value = '';
  if (result.status === 401) {
    handleExpired();
    return;
  }
  if (result.status === 429) {
    setReauthError('Too many attempts. Wait and try again.');
    return;
  }
  if (result.ok && result.data && result.data.ok) {
    resolveReauth(true);
    return;
  }
  setReauthError('Incorrect password.');
}

function wireForms() {
  if (els.loginForm) {
    els.loginForm.addEventListener('submit', submitLogin);
  }
  if (els.logout) {
    els.logout.addEventListener('click', submitLogout);
  }
  els.settingForms.forEach((form) => {
    form.addEventListener('submit', submitSettingForm);
  });
  document.querySelectorAll('input[name="rotation"]').forEach((input) => {
    input.addEventListener('change', updateRotationWarning);
  });
  if (els.mqttTest) els.mqttTest.addEventListener('click', testMqtt);
  if (els.mqttForm) {
    els.mqttForm.addEventListener('submit', submitMqtt);
    els.mqttForm.addEventListener('input', invalidateMqttTest);
    els.mqttForm.addEventListener('change', invalidateMqttTest);
  }
  if (els.prusaForm) els.prusaForm.addEventListener('submit', submitPrusa);
  if (els.copyLocalAccess) els.copyLocalAccess.addEventListener('click', copyLocalAccess);
  if (els.liveToggle) els.liveToggle.addEventListener('click', toggleLivePause);
  if (els.liveDownload) els.liveDownload.addEventListener('click', downloadLiveSnapshot);
  if (els.webrtcConnect) els.webrtcConnect.addEventListener('click', toggleLocalWebrtc);
  if (els.reauthForm) els.reauthForm.addEventListener('submit', submitReauth);
  if (els.reauthCancel) {
    els.reauthCancel.addEventListener('click', () => resolveReauth(false));
  }
  if (els.systemUpdateCheck) {
    els.systemUpdateCheck.addEventListener('click', checkForUpdate);
  }
  if (els.systemUpdateInstall) {
    els.systemUpdateInstall.addEventListener('click', installUpdate);
  }
  if (els.systemDiagnosticsLoad) {
    els.systemDiagnosticsLoad.addEventListener('click', loadDiagnostics);
  }
  if (els.systemDiagnosticsDownload) {
    els.systemDiagnosticsDownload.addEventListener('click', downloadDiagnostics);
  }
  if (els.systemSshSave) els.systemSshSave.addEventListener('click', saveSsh);
  if (els.systemRecovery) els.systemRecovery.addEventListener('click', enterRecovery);
  if (els.systemReboot) els.systemReboot.addEventListener('click', rebootDevice);
  if (els.systemReset) els.systemReset.addEventListener('click', factoryReset);
  if (els.systemResetPhrase) {
    els.systemResetPhrase.addEventListener('input', resetPhraseReady);
  }
  wireTimelapse();
}

function wireVisibility() {
  document.addEventListener('visibilitychange', () => {
    if (!SESSION_STATE.csrf) return;
    if (document.visibilityState === 'visible') {
      pollDashboard();
    } else {
      scheduleDashboard();
    }
    syncLiveMonitor();
    // Live WebRTC is manual-connect only (no auto-reconnect), but a hidden
    // tab should not keep costing the Pi a GStreamer pipeline and a viewer
    // slot the user cannot see.
    if (document.visibilityState !== 'visible') disconnectLocalWebrtc();
  });
}

function init() {
  cacheElements();
  wireNavigation();
  wireForms();
  wireVisibility();
  showBoot();
  probeSession();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
