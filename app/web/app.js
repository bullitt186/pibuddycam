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

import { submitSettingForm, syncCameraView, updateRotationWarning } from './camera.js?v=__ASSET_VERSION__';
import { SESSION_STATE, cacheElements, cancelPendingReauth, clearSession, els, handleExpired, request, resolveReauth, setBusy, submitReauth } from './common.js?v=__ASSET_VERSION__';
import { wireGpio } from './gpio.js?v=__ASSET_VERSION__';
import { copyLocalAccess, invalidateMqttTest, loadIntegrations, refreshTokenBanner, renderLocalAccess, submitMqtt, submitPrusa, testMqtt } from './integrations.js?v=__ASSET_VERSION__';
import { disconnectLocalWebrtc, downloadLiveSnapshot, startLiveMonitor, stopLiveMonitor, syncLiveMonitor, toggleLivePause, toggleLocalWebrtc } from './live.js?v=__ASSET_VERSION__';
import { loadNetwork, wireNetwork } from './network.js?v=__ASSET_VERSION__';
import { pollDashboard, scheduleDashboard, startDashboardPolling, stopDashboardPolling } from './overview.js?v=__ASSET_VERSION__';
import { checkForUpdate, downloadDiagnostics, enterRecovery, factoryReset, installUpdate, loadDiagnostics, loadSystem, rebootDevice, resetPhraseReady, saveSsh, stopUpdatePolling } from './system.js?v=__ASSET_VERSION__';
import { loadTimelapseExtras, loadTimelapses, stopTimelapsePolling, wireTimelapse } from './timelapse.js?v=__ASSET_VERSION__';


function showBoot() {
  if (els.boot) els.boot.hidden = false;
  if (els.main) els.main.hidden = true;
  if (els.loginView) els.loginView.hidden = true;
  if (els.appView) els.appView.hidden = true;
}

export function showLogin(message) {
  stopDashboardPolling();
  stopLiveMonitor();
  disconnectLocalWebrtc();
  stopTimelapsePolling();
  stopUpdatePolling();
  if (els.timelapseBuild) els.timelapseBuild.disabled = false;
  if (els.reauthDialog && els.reauthDialog.open) els.reauthDialog.close();
  // A pending re-auth promise must settle (as cancelled) before the dialog is
  // discarded, or a sensitive action would await forever after an expiry.
  cancelPendingReauth();
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
  refreshTokenBanner();
  startDashboardPolling();
}

function setLoginError(message) {
  if (!els.loginError) return;
  els.loginError.textContent = message;
  els.loginError.hidden = !message;
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
    loadTimelapseExtras();
  } else if (name === 'system') {
    loadSystem();
    loadNetwork();
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
  if (els.prusaTokenBannerOpen) {
    els.prusaTokenBannerOpen.addEventListener('click', () => selectView('integrations'));
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
  wireNetwork();
  wireGpio();
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

export function init() {
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