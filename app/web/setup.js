/*
 * PiBuddyCam setup wizard (captive portal, unclaimed device only).
 *
 * No framework, no CDN, no build step, and no browser storage: the wizard state
 * lives server-side in the setup session, so a reload or a captive-portal
 * browser that is closed and reopened resumes where it left off. The page
 * talks to the public setup API only:
 *
 *   GET  /setup/state       -> redacted wizard state (steps, summary, scan list)
 *   POST /setup/step/<n>    -> validate + apply one wizard step
 *   POST /setup/wifi/scan   -> run the Wi-Fi scan, return the network list
 *   POST /setup/finish      -> stop the setup AP, join Wi-Fi, start the camera
 *   POST /api/mqtt/test     -> non-persistent broker test
 *
 * Every POST is JSON (the server refuses anything else). Secrets typed here are
 * sent once and never read back: the state only reports whether one is set.
 */

/* Wizard step numbers (setup_wizard.STEP_ORDER index + 1). */
const STEP = {
  status: 1,
  imager_prefill: 2,
  wifi: 3,
  prusa_token: 4,
  fingerprint: 5,
  admin_password: 6,
  mqtt: 7,
  summary: 8,
  persist: 9,
};

/* UI screens in order, with the wizard steps each one completes. */
const SCREENS = [
  { id: 'welcome', steps: ['status', 'imager_prefill'] },
  { id: 'wifi', steps: ['wifi'] },
  { id: 'prusa', steps: ['prusa_token'] },
  { id: 'password', steps: ['admin_password'] },
  { id: 'options', steps: ['fingerprint', 'mqtt'] },
  { id: 'review', steps: [] },
];

const REQUEST_TIMEOUT_MS = 20000;
/* The finish request usually never answers: the AP it travels over is stopped. */
const FINISH_TIMEOUT_MS = 25000;
const REDACTED = '<redacted>';

const state = {
  data: null,
  screen: null,
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ */
/* transport                                                           */
/* ------------------------------------------------------------------ */

/**
 * One same-origin JSON request. Never throws: a network failure or timeout is
 * reported as status 0. A non-JSON answer (e.g. the console's HTML after a
 * redirect) is reported with ``html: true``.
 */
async function call(path, { method = 'GET', body, timeout = REQUEST_TIMEOUT_MS } = {}) {
  const init = { method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, { ...init, signal: controller.signal });
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (_error) {
      data = null;
    }
    const type = response.headers.get('Content-Type') || '';
    return {
      ok: response.ok, status: response.status, data, text,
      html: type.includes('text/html'),
    };
  } catch (_error) {
    return { ok: false, status: 0, data: null, text: '', html: false };
  } finally {
    clearTimeout(timer);
  }
}

function reasonOf(result, fallback) {
  if (result.status === 0) return 'The camera did not answer. Check that you are still on the setup network.';
  const data = result.data || {};
  const reason = data.reason || data.error || (result.html ? '' : result.text);
  return (reason && String(reason).slice(0, 300)) || fallback;
}

async function submitStep(name, body = {}) {
  return call(`/setup/step/${STEP[name]}`, { method: 'POST', body });
}

async function loadState() {
  const result = await call('/setup/state');
  if (result.html || result.status === 409) {
    // Setup is closed (device already claimed): the server sent the console.
    window.location.assign('/admin');
    return false;
  }
  if (!result.ok || !result.data) return false;
  state.data = result.data;
  return true;
}

/* ------------------------------------------------------------------ */
/* helpers                                                             */
/* ------------------------------------------------------------------ */

function completed(step) {
  return Boolean(state.data && Array.isArray(state.data.done_steps)
    && state.data.done_steps.includes(STEP[step]));
}

function summary() {
  return (state.data && state.data.summary) || {};
}

function saved(flag) {
  return Boolean(state.data && state.data.saved && state.data.saved[flag]);
}

function setError(id, message) {
  const el = $(id);
  if (!el) return;
  el.textContent = message || '';
  el.hidden = !message;
}

function setBusy(button, busy, label) {
  if (!button) return;
  if (busy) {
    button.dataset.label = button.textContent;
    button.textContent = label;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
  } else {
    button.textContent = button.dataset.label || button.textContent;
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
}

function setChip(el, text, kind) {
  el.textContent = text;
  el.className = `chip chip--${kind}`;
}

function firstOpenScreen() {
  for (const screen of SCREENS) {
    if (!screen.steps.every(completed)) return screen.id;
  }
  return 'review';
}

/* ------------------------------------------------------------------ */
/* navigation                                                          */
/* ------------------------------------------------------------------ */

function show(id) {
  state.screen = id;
  document.querySelectorAll('[data-screen]').forEach((section) => {
    section.hidden = section.dataset.screen !== id;
  });
  const index = SCREENS.findIndex((screen) => screen.id === id);
  const stepper = $('setup-stepper');
  stepper.hidden = index < 0;
  stepper.querySelectorAll('[data-step-for]').forEach((item, position) => {
    item.classList.toggle('stepper__item--done', index >= 0 && position < index);
    if (position === index) item.setAttribute('aria-current', 'step');
    else item.removeAttribute('aria-current');
  });
  const enter = ENTER[id];
  if (enter) enter();
  const heading = document.querySelector(`[data-screen="${id}"] h2`);
  if (heading) {
    heading.setAttribute('tabindex', '-1');
    heading.focus({ preventScroll: true });
  }
  window.scrollTo(0, 0);
}

function back() {
  const index = SCREENS.findIndex((screen) => screen.id === state.screen);
  if (index > 0) show(SCREENS[index - 1].id);
}

function next() {
  const index = SCREENS.findIndex((screen) => screen.id === state.screen);
  if (index >= 0 && index + 1 < SCREENS.length) show(SCREENS[index + 1].id);
}

/* ------------------------------------------------------------------ */
/* screen: welcome                                                     */
/* ------------------------------------------------------------------ */

function renderStatus() {
  const status = summary().status || {};
  const storage = $('status-storage');
  const camera = $('status-camera');
  if (!completed('status')) {
    setChip(storage, 'Unknown', 'muted');
    setChip(camera, 'Unknown', 'muted');
    return;
  }
  if (status.storage_ready) setChip(storage, 'Ready', 'ok');
  else setChip(storage, 'Not ready', 'warn');
  if (status.camera_ok) {
    const sensors = Number(status.sensors) || 0;
    setChip(camera, sensors > 1 ? `${sensors} detected` : 'Detected', 'ok');
  } else {
    setChip(camera, 'Not detected', 'warn');
  }
  const notes = [];
  if (!status.storage_ready) notes.push('The data partition is not ready yet; settings may not survive a reboot.');
  if (!status.camera_ok) {
    notes.push(`No camera module was found${status.camera_reason ? ` (${status.camera_reason})` : ''}. Check the ribbon cable; you can still finish setup.`);
  }
  $('status-detail').textContent = notes.join(' ');
  $('status-recheck').hidden = Boolean(status.camera_ok && status.storage_ready);
}

async function recheckStatus() {
  const button = $('status-recheck');
  setBusy(button, true, 'Checking…');
  const result = await submitStep('status', {});
  if (result.ok) await loadState();
  setBusy(button, false);
  renderStatus();
}

async function startWizard() {
  const button = $('welcome-next');
  setError('welcome-error', '');
  setBusy(button, true, 'Starting…');
  if (!completed('imager_prefill')) {
    const result = await submitStep('imager_prefill', {});
    if (!result.ok) {
      setBusy(button, false);
      setError('welcome-error', reasonOf(result, 'Setup could not start.'));
      return;
    }
    await loadState();
  }
  setBusy(button, false);
  next();
}

/* ------------------------------------------------------------------ */
/* screen: Wi-Fi                                                       */
/* ------------------------------------------------------------------ */

function signalLabel(signal) {
  if (signal >= 70) return 'Excellent';
  if (signal >= 50) return 'Good';
  if (signal >= 30) return 'Fair';
  return 'Weak';
}

function renderNetworks(networks) {
  const list = $('wifi-networks');
  const current = $('wifi-ssid').value;
  list.textContent = '';
  networks.forEach((network, index) => {
    const label = document.createElement('label');
    label.className = 'network-list__item';
    const input = document.createElement('input');
    input.type = 'radio';
    input.name = 'wifi-network';
    input.value = network.ssid;
    input.id = `wifi-network-${index}`;
    input.checked = network.ssid === current;
    input.addEventListener('change', () => {
      $('wifi-ssid').value = network.ssid;
      $('wifi-psk').focus();
    });
    const name = document.createElement('span');
    name.className = 'network-list__ssid';
    name.textContent = network.ssid;
    const meta = document.createElement('span');
    meta.className = 'network-list__meta';
    meta.textContent = `${signalLabel(network.signal)} · ${network.secured ? 'Secured' : 'Open'}`;
    label.append(input, name, meta);
    list.append(label);
  });
  list.hidden = networks.length === 0;
}

async function scanNetworks() {
  const button = $('wifi-scan');
  const status = $('wifi-scan-status');
  setBusy(button, true, 'Scanning…');
  status.textContent = 'Scanning for networks…';
  const result = await call('/setup/wifi/scan', { method: 'POST', body: {}, timeout: 45000 });
  setBusy(button, false);
  const data = result.data || {};
  const networks = Array.isArray(data.networks) ? data.networks : [];
  renderNetworks(networks);
  if (!result.ok || !data.ok) {
    status.textContent = 'The scan did not work while the setup network is running. Type the network name below.';
  } else if (networks.length === 0) {
    status.textContent = 'No networks found. Type the network name below.';
  } else {
    status.textContent = `${networks.length} network${networks.length === 1 ? '' : 's'} found.`;
  }
}

function enterWifi() {
  const wifi = summary().wifi || {};
  const ssid = $('wifi-ssid');
  if (!ssid.value && wifi.ssid) ssid.value = wifi.ssid;
  const pskSaved = completed('wifi') && saved('wifi_secured');
  $('wifi-psk-hint').textContent = pskSaved
    ? 'A password is saved. Leave empty to keep it.'
    : '8 to 63 characters. Leave empty only for an open network.';
  const networks = (state.data && state.data.networks) || [];
  renderNetworks(networks);
  if (networks.length === 0 && state.data && state.data.scan_available && !state.scanned) {
    state.scanned = true;
    scanNetworks();
  } else if (state.data && !state.data.scan_available) {
    $('wifi-scan').hidden = true;
    $('wifi-scan-status').textContent = 'Scanning is not available. Type the network name below.';
  }
}

async function submitWifi(event) {
  event.preventDefault();
  setError('wifi-error', '');
  const ssid = $('wifi-ssid').value.trim();
  const psk = $('wifi-psk').value;
  const current = summary().wifi || {};
  if (!ssid) {
    setError('wifi-error', 'Enter or pick a network name.');
    $('wifi-ssid').focus();
    return;
  }
  if (psk && (psk.length < 8 || psk.length > 63)) {
    setError('wifi-error', 'The Wi-Fi password must be 8 to 63 characters.');
    $('wifi-psk').focus();
    return;
  }
  // Unchanged network with a saved password and an empty field: keep it.
  if (completed('wifi') && !psk && ssid === current.ssid && saved('wifi_secured')) {
    next();
    return;
  }
  const button = $('wifi-next');
  setBusy(button, true, 'Saving…');
  const result = await submitStep('wifi', { ssid, psk });
  setBusy(button, false);
  if (!result.ok) {
    setError('wifi-error', reasonOf(result, 'The Wi-Fi settings were rejected.'));
    return;
  }
  $('wifi-psk').value = '';
  await loadState();
  next();
}

/* ------------------------------------------------------------------ */
/* screen: Prusa Connect                                               */
/* ------------------------------------------------------------------ */

function enterPrusa() {
  $('prusa-hint').textContent = saved('prusa_ready')
    ? 'A token is saved. Leave empty to keep it, or paste a new one.'
    : 'The token stays on the camera and is never shown again.';
}

async function submitPrusa(event) {
  event.preventDefault();
  setError('prusa-error', '');
  const token = $('prusa-token').value.trim();
  if (!token && saved('prusa_ready')) {
    next();
    return;
  }
  if (!token) {
    setError('prusa-error', 'Paste the camera token from Prusa Connect.');
    $('prusa-token').focus();
    return;
  }
  const button = $('prusa-next');
  setBusy(button, true, 'Saving…');
  const result = await submitStep('prusa_token', { source: 'manual', token });
  setBusy(button, false);
  if (!result.ok) {
    setError('prusa-error', reasonOf(result, 'The token was rejected.'));
    return;
  }
  $('prusa-token').value = '';
  await loadState();
  next();
}

/* ------------------------------------------------------------------ */
/* screen: password                                                    */
/* ------------------------------------------------------------------ */

function enterPassword() {
  $('admin-password-hint').textContent = saved('admin_ready')
    ? 'A password is set. Leave both fields empty to keep it.'
    : 'At least 8 characters.';
}

async function submitPassword(event) {
  event.preventDefault();
  setError('password-error', '');
  const password = $('admin-password').value;
  const confirm = $('admin-password-confirm').value;
  if (!password && !confirm && saved('admin_ready')) {
    next();
    return;
  }
  if (password.length < 8) {
    setError('password-error', 'The password must be at least 8 characters.');
    $('admin-password').focus();
    return;
  }
  if (password !== confirm) {
    setError('password-error', 'The passwords do not match.');
    $('admin-password-confirm').focus();
    return;
  }
  const button = $('password-next');
  setBusy(button, true, 'Saving…');
  const result = await submitStep('admin_password', { password, confirm });
  setBusy(button, false);
  if (!result.ok) {
    setError('password-error', reasonOf(result, 'The password was rejected.'));
    return;
  }
  $('admin-password').value = '';
  $('admin-password-confirm').value = '';
  await loadState();
  next();
}

/* ------------------------------------------------------------------ */
/* screen: options                                                     */
/* ------------------------------------------------------------------ */

function syncMqttFields() {
  $('mqtt-fields').hidden = !$('mqtt-enabled').checked;
}

function enterOptions() {
  const mqtt = summary().mqtt || {};
  if (completed('mqtt') && !$('mqtt-uri').value) {
    $('mqtt-enabled').checked = Boolean(mqtt.enabled);
    if (mqtt.enabled && mqtt.uri && mqtt.uri !== REDACTED) $('mqtt-uri').value = mqtt.uri;
  }
  $('mqtt-test').hidden = !(state.data && state.data.mqtt_test_available);
  syncMqttFields();
}

function mqttBody() {
  return {
    uri: $('mqtt-uri').value.trim(),
    username: $('mqtt-username').value.trim(),
    password: $('mqtt-password').value,
  };
}

async function testMqtt() {
  const status = $('mqtt-test-status');
  const body = mqttBody();
  status.hidden = false;
  if (!body.uri) {
    status.className = 'form-status form-status--error';
    status.textContent = 'Enter the broker URI first.';
    return;
  }
  const button = $('mqtt-test');
  setBusy(button, true, 'Testing…');
  status.className = 'form-status form-status--info';
  status.textContent = 'Connecting to the broker…';
  const result = await call('/api/mqtt/test', { method: 'POST', body, timeout: 30000 });
  setBusy(button, false);
  const data = result.data || {};
  if (result.ok && data.ok) {
    status.className = 'form-status form-status--ok';
    status.textContent = 'Connected to the broker.';
  } else {
    status.className = 'form-status form-status--error';
    status.textContent = reasonOf(result, 'The broker test failed.');
  }
}

async function submitOptions(event) {
  event.preventDefault();
  setError('options-error', '');
  const button = $('options-next');
  setBusy(button, true, 'Saving…');
  // The fingerprint step always runs: an empty value persists the stable
  // MAC-derived fingerprint instead of leaving it to the runtime.
  let result = await submitStep('fingerprint', { fingerprint: $('fingerprint').value.trim() });
  if (result.ok) {
    const enabled = $('mqtt-enabled').checked;
    result = await submitStep('mqtt', enabled ? { enabled: true, ...mqttBody() } : { enabled: false });
  }
  setBusy(button, false);
  if (!result.ok) {
    setError('options-error', reasonOf(result, 'The optional settings were rejected.'));
    return;
  }
  await loadState();
  next();
}

/* ------------------------------------------------------------------ */
/* screen: review + finish                                             */
/* ------------------------------------------------------------------ */

function reviewRow(list, label, value, screen) {
  const row = document.createElement('div');
  row.className = 'setup__review-row';
  const dt = document.createElement('dt');
  dt.textContent = label;
  const dd = document.createElement('dd');
  const text = document.createElement('span');
  text.textContent = value;
  dd.append(text);
  if (screen) {
    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'btn btn--ghost btn--small';
    edit.textContent = 'Change';
    edit.setAttribute('aria-label', `Change ${label}`);
    edit.addEventListener('click', () => show(screen));
    dd.append(edit);
  }
  row.append(dt, dd);
  list.append(row);
}

function enterReview() {
  const data = summary();
  const status = data.status || {};
  const wifi = data.wifi || {};
  const mqtt = data.mqtt || {};
  const list = $('review-list');
  list.textContent = '';
  reviewRow(list, 'Camera module', status.camera_ok ? 'Detected' : 'Not detected', null);
  reviewRow(list, 'Wi-Fi network', `${wifi.ssid || '—'}${saved('wifi_secured') ? ' (password saved)' : ' (open network)'}`, 'wifi');
  reviewRow(list, 'Prusa Connect token', saved('prusa_ready') ? 'Saved' : 'Missing', 'prusa');
  reviewRow(list, 'Admin password', saved('admin_ready') ? 'Set' : 'Missing', 'password');
  reviewRow(list, 'MQTT', mqtt.enabled ? (mqtt.uri || 'Enabled') : 'Off', 'options');
  reviewRow(list, 'Fingerprint', saved('fingerprint_pinned') ? 'Stored' : 'Derived at runtime', 'options');
  reviewRow(list, 'Console address', (state.data && state.data.admin_url) || '—', null);
  const missing = ['wifi', 'prusa_token', 'admin_password', 'fingerprint', 'mqtt'].filter((step) => !completed(step));
  $('review-finish').disabled = missing.length > 0;
  setError('review-error', missing.length ? 'Some steps are not complete yet. Go back and fill them in.' : '');
}

function renderDone(failedQuietly) {
  const data = state.data || {};
  const wifi = summary().wifi || {};
  $('done-ssid').textContent = wifi.ssid || 'your Wi-Fi';
  $('done-url').textContent = data.admin_url || "the camera's address";
  $('done-setup-ssid').textContent = data.setup_ssid || 'PiBuddyCam-Setup';
  if (failedQuietly) {
    $('done-lede').textContent = 'The setup network was switched off as planned. The camera is joining your Wi-Fi and will appear in Prusa Connect within a minute or two.';
  }
  show('done');
}

async function finish() {
  setError('review-error', '');
  const button = $('review-finish');
  setBusy(button, true, 'Saving…');
  await submitStep('summary', {});
  const persisted = await submitStep('persist', {});
  setBusy(button, false);
  if (!persisted.ok) {
    setError('review-error', reasonOf(persisted, 'The configuration could not be saved.'));
    return;
  }
  await loadState();
  show('finishing');
  $('finishing-text').textContent = 'Switching off the setup network and joining your Wi-Fi…';
  const result = await call('/setup/finish', { method: 'POST', body: {}, timeout: FINISH_TIMEOUT_MS });
  if (result.ok) {
    renderDone(false);
    return;
  }
  if (result.status === 0) {
    // Expected: the answer travelled over the access point that was just stopped.
    renderDone(true);
    return;
  }
  show('review');
  setError('review-error', reasonOf(result, 'The camera could not start.'));
}

/* ------------------------------------------------------------------ */
/* boot                                                                */
/* ------------------------------------------------------------------ */

const ENTER = {
  welcome: renderStatus,
  wifi: enterWifi,
  prusa: enterPrusa,
  password: enterPassword,
  options: enterOptions,
  review: enterReview,
};

function wire() {
  document.querySelectorAll('[data-back]').forEach((button) => button.addEventListener('click', back));
  $('welcome-next').addEventListener('click', startWizard);
  $('status-recheck').addEventListener('click', recheckStatus);
  $('wifi-form').addEventListener('submit', submitWifi);
  $('wifi-scan').addEventListener('click', scanNetworks);
  $('wifi-psk-show').addEventListener('change', (event) => {
    $('wifi-psk').type = event.target.checked ? 'text' : 'password';
  });
  $('prusa-form').addEventListener('submit', submitPrusa);
  $('password-form').addEventListener('submit', submitPassword);
  $('admin-password-show').addEventListener('change', (event) => {
    const type = event.target.checked ? 'text' : 'password';
    $('admin-password').type = type;
    $('admin-password-confirm').type = type;
  });
  $('options-form').addEventListener('submit', submitOptions);
  $('mqtt-enabled').addEventListener('change', syncMqttFields);
  $('mqtt-test').addEventListener('click', testMqtt);
  $('review-finish').addEventListener('click', finish);
}

async function boot() {
  wire();
  let loaded = await loadState();
  if (loaded && !completed('status')) {
    // Step 1 runs the storage check and the camera probe.
    await submitStep('status', {});
    loaded = await loadState();
  }
  $('setup-boot').hidden = true;
  $('setup-main').hidden = false;
  if (!loaded) {
    show('welcome');
    setError('welcome-error', 'The setup service did not answer. Reload the page to try again.');
    return;
  }
  $('setup-ssid').textContent = state.data.setup_ssid || 'PiBuddyCam-Setup';
  show(firstOpenScreen());
}

boot();
