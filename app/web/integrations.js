/*
 * PiBuddyCam local console: Integrations: Prusa Connect and MQTT / Home Assistant.
 *
 * ES module of the console (no bundler). Imports carry the content-hash query the
 * server substitutes for __ASSET_VERSION__, so a cached module can never be older
 * than the entry point that loads it.
 */

import { els, handleExpired, request, requestReauth, setBusy, setFormBusy, setFormStatus, setText, setTextValue } from './common.js?v=__ASSET_VERSION__';


/* Last authoritative dashboard document, used to converge the Camera forms
 * after Prusa/MQTT changes without an extra round-trip. */

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

function setTokenBanner(configured) {
  if (els.prusaTokenBanner) els.prusaTokenBanner.hidden = configured === true;
}

/** Show the "no Prusa token" banner when the camera was set up with "Later". */
export function refreshTokenBanner() {
  request('/api/integrations').then((result) => {
    if (result.ok && result.data && result.data.ok === true) {
      setTokenBanner((result.data.prusa || {}).token_configured);
    }
  });
}

export function loadIntegrations() {
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
  setTokenBanner(prusa.token_configured);
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

export function renderLocalAccess() {
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
export function invalidateMqttTest(event) {
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

export async function testMqtt() {
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

export async function submitMqtt(event) {
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

export async function submitPrusa(event) {
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
    const warning = data.active
      ? 'Saved. The camera restarted and registers with Prusa Connect now.'
      : ((data.warnings || [])[0] || 'Saved.');
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

export async function copyLocalAccess() {
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