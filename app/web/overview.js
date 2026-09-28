/*
 * PiBuddyCam local console: Overview dashboard (read-only, authenticated): polling, status chips and metrics.
 *
 * ES module of the console (no bundler). Imports carry the content-hash query the
 * server substitutes for __ASSET_VERSION__, so a cached module can never be older
 * than the entry point that loads it.
 */

import { applySettings } from './camera.js?v=__ASSET_VERSION__';
import { SESSION_STATE, els, formatAge, formatBytes, formatDuration, handleExpired, setText, shared } from './common.js?v=__ASSET_VERSION__';
import { renderGpioStatus } from './gpio.js?v=__ASSET_VERSION__';
import { refreshSystemView } from './system.js?v=__ASSET_VERSION__';


/* Dashboard polling cadence: fast while the tab is visible, slower in the
 * background. A new poll aborts any still-in-flight request. */
const DASHBOARD_INTERVAL_VISIBLE = 5000;
const DASHBOARD_INTERVAL_HIDDEN = 30000;

const dashboard = {
  timer: null,
  controller: null,
  inflight: false,
};

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
  shared.dashboard = data;
  const runtime = data.runtime || {};
  renderFreshness(runtime);
  renderMetrics(data);
  renderStatusChips(data);
  applySettings(data.settings);
  renderGpioStatus(data);
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
  refreshSystemView();
}

export function stopDashboardPolling() {
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

export function scheduleDashboard() {
  if (dashboard.timer !== null) clearTimeout(dashboard.timer);
  const delay = document.visibilityState === 'visible'
    ? DASHBOARD_INTERVAL_VISIBLE
    : DASHBOARD_INTERVAL_HIDDEN;
  dashboard.timer = setTimeout(pollDashboard, delay);
}

export function startDashboardPolling() {
  stopDashboardPolling();
  renderDashboardLoading();
  pollDashboard();
}

export async function pollDashboard() {
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