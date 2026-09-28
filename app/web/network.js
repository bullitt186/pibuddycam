/*
 * PiBuddyCam local console: Network card: Wi-Fi, IP, hostname and time servers.
 *
 * ES module of the console (no bundler). Imports carry the content-hash query the
 * server substitutes for __ASSET_VERSION__, so a cached module can never be older
 * than the entry point that loads it.
 */

import { SESSION_STATE, handleExpired, isFormPending, request, requestReauth, setBusy, setFormBusy, setFormStatus, setNumberValue, setRadioValue, setText, setTextValue, shared } from './common.js?v=__ASSET_VERSION__';


/* ------------------------------------------------------------------ */
/* Network settings (System view)                                      */
/* ------------------------------------------------------------------ */

const NETWORK_POLL_MS = 3000;
/* About three minutes: the root transaction waits up to a minute and may
 * restore the old profile (and, as a last resort, start the setup hotspot). */
const NETWORK_POLL_MAX = 60;
const NETWORK_SCAN_POLL_MS = 1500;
const NETWORK_SCAN_POLL_MAX = 12;

const netUi = {
  zones: null,
  zonesLoading: false,
  zoneDirty: false,
  currentZone: '',
  dirty: false,
  timer: null,
  polls: 0,
  baseline: null,
  scanTimer: null,
};

function netEl(id) {
  return document.getElementById(id);
}

function splitList(text) {
  return String(text || '').split(/[\s,]+/).map((item) => item.trim()).filter(Boolean);
}

function setNetworkResult(kind, text, href) {
  const box = netEl('network-result');
  if (!box) return;
  box.textContent = text || '';
  if (text && href) {
    box.appendChild(document.createTextNode(' '));
    const link = document.createElement('a');
    link.href = href;
    link.textContent = href;
    link.rel = 'noopener noreferrer';
    box.appendChild(link);
  }
  box.className = kind === 'ok'
    ? 'notice notice--trusted' : (kind === 'error' ? 'notice notice--error' : 'notice notice--warn');
  box.hidden = !text;
}

function toggleStaticFields() {
  const manual = document.querySelector('input[name="network_ipv4"][value="manual"]');
  const fields = netEl('network-static');
  if (fields) fields.hidden = !(manual && manual.checked);
}

function renderNetwork(data, options = {}) {
  const link = data.link || {};
  setText(netEl('network-current-ssid'), link.ssid || '—');
  const address = link.address
    ? `${link.address}${link.prefix ? `/${link.prefix}` : ''}` : '—';
  setText(netEl('network-current-address'), address);
  setText(netEl('network-current-gateway'), link.gateway || '—');
  setText(netEl('network-current-dns'), (link.dns || []).join(', ') || '—');
  setText(netEl('network-current-hostname'), data.hostname || '—');
  const time = data.time || {};
  setText(
    netEl('network-current-time'),
    (time.synchronized
      ? `synchronized${time.server ? ` (${time.server})` : ''}`
      : 'not synchronized') + (time.timezone ? ` · ${time.timezone}` : ''),
  );
  setText(
    netEl('network-status'),
    link.connected ? 'Connected.' : 'The Wi-Fi link is down or not readable.',
  );
  if (options.keepForm || netUi.dirty) return;
  setTextValue('network-ssid', link.ssid);
  const ipv4 = link.ipv4 || {};
  setRadioValue('network_ipv4', ipv4.method === 'manual' ? 'manual' : 'auto');
  if (ipv4.method === 'manual') {
    setTextValue('network-address', ipv4.address);
    setNumberValue('network-prefix', ipv4.prefix);
    setTextValue('network-gateway', ipv4.gateway);
    setTextValue('network-dns', (ipv4.dns || []).join(', '));
  }
  setTextValue('network-hostname', data.hostname);
  setTextValue('network-ntp', (data.ntp_servers || []).join(', '));
  netUi.currentZone = time.timezone || '';
  selectTimezone();
  const psk = netEl('network-psk');
  if (psk) {
    psk.placeholder = data.psk_set
      ? 'Leave blank to keep the current password'
      : 'Wi-Fi password (blank for an open network)';
  }
  toggleStaticFields();
}

export async function loadNetwork() {
  const card = netEl('network-card');
  if (!card || !SESSION_STATE.csrf) return null;
  const result = await request('/api/network');
  if (result.status === 401) {
    handleExpired();
    return null;
  }
  if (result.status === 501) {
    card.hidden = true;
    return null;
  }
  card.hidden = false;
  if (!result.ok || !result.data || result.data.ok !== true) {
    setText(netEl('network-status'), 'Network status is unavailable.');
    return null;
  }
  renderNetwork(result.data);
  loadTimezones();
  return result.data;
}

/** Return ``{body}`` for the apply request or ``{error}`` for the form. */
function collectNetworkBody() {
  const ssid = (netEl('network-ssid') || {}).value || '';
  if (!ssid.trim()) return { error: 'Enter the network name.' };
  const psk = (netEl('network-psk') || {}).value || '';
  if (psk && (psk.length < 8 || psk.length > 63)) {
    return { error: 'The Wi-Fi password must be 8 to 63 characters.' };
  }
  const manual = document.querySelector('input[name="network_ipv4"][value="manual"]');
  const body = { ssid: ssid.trim(), psk, ipv4_method: manual && manual.checked ? 'manual' : 'auto' };
  if (body.ipv4_method === 'manual') {
    const prefix = Number((netEl('network-prefix') || {}).value);
    if (!Number.isInteger(prefix) || prefix < 1 || prefix > 30) {
      return { error: 'The prefix length must be between 1 and 30.' };
    }
    body.address = ((netEl('network-address') || {}).value || '').trim();
    body.prefix = prefix;
    body.gateway = ((netEl('network-gateway') || {}).value || '').trim();
    body.dns = splitList((netEl('network-dns') || {}).value);
    if (!body.address || !body.gateway) {
      return { error: 'Enter the IP address and the gateway.' };
    }
    if (body.dns.length > 3) return { error: 'At most three DNS servers.' };
  }
  return { body };
}

function stopNetworkPolling() {
  if (netUi.timer !== null) {
    clearTimeout(netUi.timer);
    netUi.timer = null;
  }
}

function consoleUrl(address) {
  return address ? `${location.protocol}//${address}/admin` : '';
}

/**
 * After the 202 the address may change, so this polls the console until the root
 * transaction reports a result. A result older than the request (``baseline``)
 * is the previous change and is ignored.
 */
function pollNetworkApply(expected) {
  stopNetworkPolling();
  netUi.polls = 0;
  const tick = async () => {
    netUi.polls += 1;
    const result = await request('/api/network');
    const data = result.ok && result.data && result.data.ok === true ? result.data : null;
    const outcome = data && data.result;
    const fresh = outcome && outcome.updated_at !== netUi.baseline;
    if (data) renderNetwork(data, { keepForm: true });
    if (fresh && outcome.state === 'applied') {
      netUi.dirty = false;
      const address = (data.link && data.link.address) || expected;
      setNetworkResult('ok', 'The new network settings are active.',
        address && address !== location.hostname ? consoleUrl(address) : '');
      renderNetwork(data);
      return;
    }
    if (fresh && (outcome.state === 'reverted' || outcome.state === 'hotspot')) {
      const why = outcome.reason ? ` (${outcome.reason})` : '';
      setNetworkResult(
        outcome.state === 'hotspot' ? 'error' : 'warn',
        outcome.state === 'hotspot'
          ? `The new settings failed${why} and the previous network did not come back. The setup hotspot was started.`
          : `The new settings did not work${why}. The camera returned to the previous settings.`,
      );
      return;
    }
    if (netUi.polls >= NETWORK_POLL_MAX) {
      const where = expected ? ` at ${consoleUrl(expected)}` : ' at its new address (see your router)';
      setNetworkResult('warn',
        `No confirmation yet. If this page does not recover, reconnect${where}.`);
      return;
    }
    setNetworkResult(
      'warn',
      data
        ? 'Applying the network change…'
        : 'The connection dropped while the camera switches networks. Waiting for it to return…',
    );
    netUi.timer = setTimeout(tick, NETWORK_POLL_MS);
  };
  netUi.timer = setTimeout(tick, NETWORK_POLL_MS);
}

async function submitNetwork(event) {
  event.preventDefault();
  const form = netEl('network-form');
  if (!form || isFormPending(form)) return;
  const collected = collectNetworkBody();
  if (collected.error) {
    setFormStatus(form, 'error', collected.error);
    return;
  }
  const ack = netEl('network-confirm');
  if (!(ack && ack.checked)) {
    setFormStatus(form, 'error', 'Check the acknowledgement first.');
    return;
  }
  // The route is re-auth gated and the body may carry the Wi-Fi password, so the
  // admin password can never travel in it: confirm first.
  const confirmed = await requestReauth();
  if (!confirmed) {
    setFormStatus(form, 'error', 'Re-authentication is required to change the network.');
    return;
  }
  const before = await request('/api/network');
  netUi.baseline = before.ok && before.data && before.data.result
    ? before.data.result.updated_at : null;
  setFormBusy(form, true);
  setFormStatus(form, 'info', 'Sending…');
  const result = await request('/api/network', {
    method: 'PUT', csrf: true, body: { ...collected.body, confirm: true },
  });
  setFormBusy(form, false);
  const psk = netEl('network-psk');
  if (psk) psk.value = '';
  if (result.status === 401) {
    handleExpired();
    return;
  }
  const data = result.data || {};
  if (result.status === 202 && data.ok) {
    setFormStatus(form, 'ok', 'Accepted.');
    setNetworkResult('warn', data.warning || 'Applying the network change…');
    pollNetworkApply(data.expected_address || '');
    return;
  }
  if (result.status === 409) {
    setFormStatus(form, 'error', 'A network change is already in progress.');
    pollNetworkApply('');
    return;
  }
  if (result.status === 501) {
    setFormStatus(form, 'error', 'Changing the network needs a newer camera image.');
    return;
  }
  setFormStatus(form, 'error', data.error || 'The network change could not be started.');
}

async function submitHostname(event) {
  event.preventDefault();
  const form = netEl('network-hostname-form');
  if (!form || isFormPending(form)) return;
  const hostname = ((netEl('network-hostname') || {}).value || '').trim();
  if (!hostname) {
    setFormStatus(form, 'error', 'Enter a hostname.');
    return;
  }
  const confirmed = await requestReauth();
  if (!confirmed) {
    setFormStatus(form, 'error', 'Re-authentication is required to change the hostname.');
    return;
  }
  setFormBusy(form, true);
  setFormStatus(form, 'info', 'Saving…');
  const result = await request('/api/network/hostname', {
    method: 'PUT', csrf: true, body: { hostname },
  });
  setFormBusy(form, false);
  if (result.status === 401) {
    handleExpired();
    return;
  }
  const data = result.data || {};
  if (result.ok && data.ok) {
    setFormStatus(form, 'ok', 'Saved. Reboot the camera to update the console certificate.');
    loadNetwork();
    return;
  }
  if (result.status === 501) {
    setFormStatus(form, 'error', 'Changing the hostname needs a newer camera image.');
    return;
  }
  setFormStatus(form, 'error', data.error || 'The hostname could not be changed.');
}

async function submitNtp(event) {
  event.preventDefault();
  const form = netEl('network-ntp-form');
  if (!form || isFormPending(form)) return;
  const servers = splitList((netEl('network-ntp') || {}).value);
  if (servers.length > 3) {
    setFormStatus(form, 'error', 'At most three time servers.');
    return;
  }
  setFormBusy(form, true);
  setFormStatus(form, 'info', 'Saving…');
  const result = await request('/api/network/ntp', {
    method: 'PUT', csrf: true, body: { ntp_servers: servers },
  });
  setFormBusy(form, false);
  if (result.status === 401) {
    handleExpired();
    return;
  }
  const data = result.data || {};
  if (result.ok && data.ok) {
    setFormStatus(
      form, 'ok',
      data.applied === false
        ? 'Saved. Applying the time servers needs a newer camera image.'
        : 'Saved.');
    loadNetwork();
    return;
  }
  setFormStatus(form, 'error', data.error || 'The time servers could not be saved.');
}

function stopNetworkScanPolling() {
  if (netUi.scanTimer !== null) {
    clearTimeout(netUi.scanTimer);
    netUi.scanTimer = null;
  }
}

async function scanNetworks() {
  const button = netEl('network-scan');
  const status = netEl('network-scan-status');
  stopNetworkScanPolling();
  setBusy(button, true, 'Scanning…');
  let attempts = 0;
  const step = async () => {
    attempts += 1;
    const result = await request('/api/network/scan');
    if (result.status === 401) {
      setBusy(button, false, 'Scan for networks');
      handleExpired();
      return;
    }
    const data = result.data || {};
    if (!result.ok) {
      setBusy(button, false, 'Scan for networks');
      setText(status, result.status === 501
        ? 'Scanning needs a newer camera image.' : 'The scan failed.');
      return;
    }
    if (data.scanning && attempts < NETWORK_SCAN_POLL_MAX) {
      netUi.scanTimer = setTimeout(step, NETWORK_SCAN_POLL_MS);
      return;
    }
    setBusy(button, false, 'Scan for networks');
    const list = netEl('network-ssid-list');
    const networks = Array.isArray(data.networks) ? data.networks : [];
    if (list) {
      list.textContent = '';
      networks.forEach((network) => {
        const option = document.createElement('option');
        option.value = network.ssid;
        option.label = `${network.ssid} · ${network.signal}%${network.secured ? '' : ' · open'}`;
        list.appendChild(option);
      });
    }
    setText(status, networks.length
      ? `${networks.length} network${networks.length === 1 ? '' : 's'} found. Pick one from the name field.`
      : 'No networks found.');
  };
  await step();
}

/** Fill the time-zone list once; the names come from the device's own tz database. */
async function loadTimezones() {
  if (netUi.zones || netUi.zonesLoading) return;
  netUi.zonesLoading = true;
  const result = await request('/api/timezones');
  netUi.zonesLoading = false;
  if (!result.ok || !result.data || !Array.isArray(result.data.zones)) return;
  netUi.zones = result.data.zones;
  const select = netEl('network-timezone');
  if (!select) return;
  select.textContent = '';
  const unset = document.createElement('option');
  unset.value = '';
  unset.textContent = 'Image default';
  select.appendChild(unset);
  netUi.zones.forEach((zone) => {
    const option = document.createElement('option');
    option.value = zone;
    option.textContent = zone;
    select.appendChild(option);
  });
  selectTimezone();
}

/** Preselect the zone the console saved, else the one the system reports. */
function selectTimezone() {
  const select = netEl('network-timezone');
  if (!select || netUi.zoneDirty || !netUi.zones) return;
  const saved = shared.dashboard && shared.dashboard.settings
    ? shared.dashboard.settings.timezone : '';
  const wanted = saved || netUi.currentZone || '';
  select.value = netUi.zones.includes(wanted) ? wanted : '';
}

async function submitTimezone(event) {
  event.preventDefault();
  const form = netEl('network-timezone-form');
  if (!form || isFormPending(form)) return;
  const zone = (netEl('network-timezone') || {}).value || '';
  setFormBusy(form, true);
  setFormStatus(form, 'info', 'Applying…');
  const result = await request('/api/settings', {
    method: 'PATCH', csrf: true, body: { field: 'timezone', value: zone },
  });
  setFormBusy(form, false);
  if (result.status === 401) {
    handleExpired();
    return;
  }
  const data = result.data || {};
  if (result.ok && data.ok) {
    netUi.zoneDirty = false;
    if (shared.dashboard && data.settings) shared.dashboard.settings = data.settings;
    setFormStatus(form, 'ok', zone ? 'Saved.' : 'Saved. The image default applies after a reboot.');
    loadNetwork();
    return;
  }
  if (result.status === 503) {
    setFormStatus(form, 'error', 'The camera runtime is unavailable. Try again when it is running.');
    return;
  }
  setFormStatus(form, 'error', data.error || 'The time zone could not be set.');
}

export function wireNetwork() {
  const zoneForm = netEl('network-timezone-form');
  if (zoneForm) {
    zoneForm.addEventListener('submit', submitTimezone);
    zoneForm.addEventListener('change', () => { netUi.zoneDirty = true; });
  }
  const form = netEl('network-form');
  if (form) {
    form.addEventListener('submit', submitNetwork);
    form.addEventListener('input', () => { netUi.dirty = true; });
    form.addEventListener('change', () => {
      netUi.dirty = true;
      toggleStaticFields();
    });
  }
  const hostname = netEl('network-hostname-form');
  if (hostname) hostname.addEventListener('submit', submitHostname);
  const ntp = netEl('network-ntp-form');
  if (ntp) ntp.addEventListener('submit', submitNtp);
  const scan = netEl('network-scan');
  if (scan) scan.addEventListener('click', scanNetworks);
}