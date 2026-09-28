/*
 * PiBuddyCam local console: System view: health, updates, diagnostics and the danger zone.
 *
 * ES module of the console (no bundler). Imports carry the content-hash query the
 * server substitutes for __ASSET_VERSION__, so a cached module can never be older
 * than the entry point that loads it.
 */

import { SESSION_STATE, els, formatBytes, formatDuration, formatEpoch, handleExpired, request, requestReauth, setBusy, setText, shared } from './common.js?v=__ASSET_VERSION__';


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

export async function loadSystem() {
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

  const dash = shared.dashboard || {};
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

export async function checkForUpdate() {
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

export async function installUpdate() {
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

export function stopUpdatePolling() {
  if (updatePollTimer) {
    clearTimeout(updatePollTimer);
    updatePollTimer = null;
  }
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

export async function loadDiagnostics() {
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

export function downloadDiagnostics() {
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

export async function saveSsh() {
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

export async function enterRecovery() {
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

export async function rebootDevice() {
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

export function resetPhraseReady() {
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

export async function factoryReset() {
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

export function refreshSystemView() {
  if (LAST_SYSTEM) renderSystem(LAST_SYSTEM, LAST_UPDATE || {});
}