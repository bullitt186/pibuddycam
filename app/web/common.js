/*
 * PiBuddyCam local console: Shared state and helpers: session state, DOM element cache, request(), formatters, form helpers and the re-authentication dialog.
 *
 * ES module of the console (no bundler). Imports carry the content-hash query the
 * server substitutes for __ASSET_VERSION__, so a cached module can never be older
 * than the entry point that loads it.
 */

import { init, showLogin } from './app.js?v=__ASSET_VERSION__';

export const shared = { dashboard: null };

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

export const SESSION_STATE = {
  csrf: null,
  mode: null,
};

/* Pending re-auth dialog resolver (true = confirmed, false = cancelled). */
let reauthResolver = null;

export const els = {};

export function cacheElements() {
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

export function setBusy(button, busy, label) {
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
export async function request(path, options = {}) {
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

export function clearSession() {
  SESSION_STATE.csrf = null;
  SESSION_STATE.mode = null;
}

/** The session-expiry path: any authenticated 401 returns to login. */
export function handleExpired() {
  clearSession();
  showLogin('Your session expired. Sign in again.');
}

/* ------------------------------------------------------------------ */
/* Overview dashboard (read-only, authenticated)                       */
/* ------------------------------------------------------------------ */

export function setText(element, text) {
  if (element) element.textContent = text;
}

export function formatBytes(bytes) {
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

export function formatDuration(seconds) {
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

export function formatAge(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return 'unknown';
  const value = Math.max(0, Math.floor(Number(seconds)));
  if (value < 60) return `${value}s`;
  if (value < 3600) return `${Math.floor(value / 60)}m`;
  return `${Math.floor(value / 3600)}h`;
}

export function formatMediaTimestamp(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return '—';
  const date = new Date(Number(seconds) * 1000);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString();
}

function settingForm(field) {
  return document.querySelector(`.setting-form[data-setting="${field}"]`);
}

export function isFormPending(form) {
  return form != null && form.dataset.pending === 'true';
}

export function anyFormPending() {
  return els.settingForms.some((form) => isFormPending(form));
}

export function setFormBusy(form, busy) {
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

export function setFormStatus(form, kind, text) {
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

export function setTextValue(id, value) {
  const input = document.getElementById(id);
  if (input && !input.disabled && document.activeElement !== input
      && value != null) {
    input.value = String(value);
  }
}

export function setCheckboxValue(name, checked) {
  const input = document.querySelector(`input[name="${name}"][type="checkbox"]`);
  if (input && document.activeElement !== input && typeof checked === 'boolean') {
    input.checked = checked;
  }
}

export function setRadioValue(name, value) {
  if (!value) return;
  const input = document.querySelector(`input[name="${name}"][value="${value}"]`);
  if (input && document.activeElement !== input) input.checked = true;
}

export function setNumberValue(id, value) {
  if (value == null || !Number.isFinite(Number(value))) return;
  setTextValue(id, value);
}

export function formatEpoch(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return '—';
  try {
    return new Date(Number(seconds) * 1000).toLocaleString();
  } catch (_error) {
    return '—';
  }
}

/* ------------------------------------------------------------------ */
/* Re-authentication dialog (reused by credential saves)               */
/* ------------------------------------------------------------------ */

/** Settle a pending re-auth promise as cancelled (used when the session ends). */
export function cancelPendingReauth() {
  if (reauthResolver) {
    const pending = reauthResolver;
    reauthResolver = null;
    pending(false);
  }
}

function setReauthError(message) {
  if (!els.reauthError) return;
  els.reauthError.textContent = message;
  els.reauthError.hidden = !message;
}

export function requestReauth() {
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

export function resolveReauth(confirmed) {
  if (els.reauthDialog && els.reauthDialog.open) els.reauthDialog.close();
  const resolver = reauthResolver;
  reauthResolver = null;
  if (resolver) resolver(confirmed);
}

export async function submitReauth(event) {
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