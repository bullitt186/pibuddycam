/*
 * Buddy3D Camera local console.
 *
 * No framework, no CDN, no build step. The module talks to the existing
 * authenticated admin API only:
 *
 *   GET  /api/session        -> { ok, mode, csrf }  (session probe / expiry)
 *   POST /api/login          -> { ok, mode, csrf }  (sets the HttpOnly cookie)
 *   POST /api/logout         -> { ok }              (requires the CSRF header)
 *   GET  /api/dashboard      -> authoritative status/metrics/settings (read)
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
  els.metricResolution = document.getElementById('metric-resolution');
  els.metricQuality = document.getElementById('metric-quality');
  els.metricWifi = document.getElementById('metric-wifi');
  els.metricTemp = document.getElementById('metric-temp');
  els.metricUptime = document.getElementById('metric-uptime');
  els.metricStorage = document.getElementById('metric-storage');
  els.metricVersion = document.getElementById('metric-version');
  els.settingForms = Array.from(document.querySelectorAll('.setting-form'));
  els.cameraState = document.getElementById('camera-state');
  els.integrationsState = document.getElementById('integrations-state');
  els.prusaForm = document.getElementById('prusa-form');
  els.mqttForm = document.getElementById('mqtt-form');
  els.mqttTest = document.getElementById('mqtt-test');
  els.mqttSaveAnyway = document.getElementById('mqtt-save-anyway');
  els.mqttUsername = document.getElementById('mqtt-username');
  els.mqttPassword = document.getElementById('mqtt-password');
  els.mqttClearUsername = document.getElementById('mqtt-clear-username');
  els.mqttClearPassword = document.getElementById('mqtt-clear-password');
  els.mqttEffective = document.getElementById('mqtt-effective');
  els.mqttRuntime = document.getElementById('mqtt-runtime');
  els.localOnvif = document.getElementById('local-onvif');
  els.localSnapshot = document.getElementById('local-snapshot');
  els.localHaRtsp = document.getElementById('local-ha-rtsp');
  els.localPrusaRtsp = document.getElementById('local-prusa-rtsp');
  els.copyLocalAccess = document.getElementById('copy-local-access');
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
  if (els.reauthDialog && els.reauthDialog.open) els.reauthDialog.close();
  reauthResolver = null;
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

/* ------------------------------------------------------------------ */
/* Camera settings (WP-UI3; AC-5/AC-6/AC-7)                            */
/* ------------------------------------------------------------------ */

const QUALITY_NAMES = { 1: 'sd', 2: 'hd', 3: 'fhd' };

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
  return window.location.hostname || 'buddy3d-camera.local';
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
    if (els.mqttPassword) els.mqttPassword.value = '';
    if (els.mqttClearUsername) els.mqttClearUsername.checked = false;
    if (els.mqttClearPassword) els.mqttClearPassword.checked = false;
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
    const token = document.getElementById('prusa-token');
    const fingerprint = document.getElementById('prusa-fingerprint');
    if (token) token.value = '';
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
  if (els.mqttTest) els.mqttTest.addEventListener('click', testMqtt);
  if (els.mqttForm) {
    els.mqttForm.addEventListener('submit', submitMqtt);
    els.mqttForm.addEventListener('input', invalidateMqttTest);
    els.mqttForm.addEventListener('change', invalidateMqttTest);
  }
  if (els.prusaForm) els.prusaForm.addEventListener('submit', submitPrusa);
  if (els.copyLocalAccess) els.copyLocalAccess.addEventListener('click', copyLocalAccess);
  if (els.reauthForm) els.reauthForm.addEventListener('submit', submitReauth);
  if (els.reauthCancel) {
    els.reauthCancel.addEventListener('click', () => resolveReauth(false));
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
