/*
 * Buddy3D Camera local console — front-end foundation.
 *
 * No framework, no CDN, no build step. The module talks to the existing
 * authenticated admin API only:
 *
 *   GET  /api/session  -> { ok, mode, csrf }  (session probe / expiry)
 *   POST /api/login    -> { ok, mode, csrf }  (sets the HttpOnly cookie)
 *   POST /api/logout   -> { ok }              (requires the CSRF header)
 *   GET  /api/dashboard-> authoritative status/metrics (read-only)
 *
 * The CSRF token is held in a module-scoped variable (memory only): it is
 * never written to localStorage, sessionStorage, a cookie, or the DOM. Every
 * authenticated request returns the login view on HTTP 401, which is the
 * session-expiry contract. The dashboard is polled only while signed in and is
 * read-only: this release performs no settings mutations.
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
  els.metricResolution = document.getElementById('metric-resolution');
  els.metricQuality = document.getElementById('metric-quality');
  els.metricWifi = document.getElementById('metric-wifi');
  els.metricTemp = document.getElementById('metric-temp');
  els.metricUptime = document.getElementById('metric-uptime');
  els.metricStorage = document.getElementById('metric-storage');
  els.metricVersion = document.getElementById('metric-version');
}

function showBoot() {
  if (els.boot) els.boot.hidden = false;
  if (els.main) els.main.hidden = true;
  if (els.loginView) els.loginView.hidden = true;
  if (els.appView) els.appView.hidden = true;
}

function showLogin(message) {
  stopDashboardPolling();
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
  const runtime = data.runtime || {};
  renderFreshness(runtime);
  renderMetrics(data);
  renderStatusChips(data);
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

function wireForms() {
  if (els.loginForm) {
    els.loginForm.addEventListener('submit', submitLogin);
  }
  if (els.logout) {
    els.logout.addEventListener('click', submitLogout);
  }
}

function wireVisibility() {
  document.addEventListener('visibilitychange', () => {
    if (!SESSION_STATE.csrf) return;
    if (document.visibilityState === 'visible') {
      pollDashboard();
    } else {
      scheduleDashboard();
    }
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
