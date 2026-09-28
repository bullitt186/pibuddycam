/*
 * PiBuddyCam local console: Camera settings forms (one coordinator mutation per form).
 *
 * ES module of the console (no bundler). Imports carry the content-hash query the
 * server substitutes for __ASSET_VERSION__, so a cached module can never be older
 * than the entry point that loads it.
 */

import { anyFormPending, handleExpired, request, setCheckboxValue, setFormBusy, setFormStatus, setNumberValue, setRadioValue, setTextValue, shared } from './common.js?v=__ASSET_VERSION__';
import { loadGpioPins, renderGpioStatus, syncGpioForm } from './gpio.js?v=__ASSET_VERSION__';


/* ------------------------------------------------------------------ */
/* Camera settings (WP-UI3; AC-5/AC-6/AC-7)                            */
/* ------------------------------------------------------------------ */
const QUALITY_NAMES = { 1: 'sd', 2: 'hd', 3: 'fhd' };
const ROTATIONS = [0, 90, 180, 270];

/** Show the 90°/270° cost warning while a transposing rotation is selected. */
export function updateRotationWarning() {
  const warning = document.getElementById('rotation-warning');
  if (!warning) return;
  const checked = document.querySelector('input[name="rotation"]:checked');
  warning.hidden = !(checked && (checked.value === '90' || checked.value === '270'));
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

/**
 * Apply the authoritative coordinator settings to the Camera forms. Skips
 * focused inputs and any form with an in-flight mutation so polling never
 * clobbers unsaved input. Called from the dashboard poll and after every
 * accepted mutation, so Prusa/MQTT/Web changes converge here too.
 */
export function applySettings(settings) {
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
  syncGpioForm(settings);
  if (settings.rtsp_mode === 1 || settings.rtsp_mode === 2) {
    setRadioValue('rtsp_mode', settings.rtsp_mode === 2 ? 'enabled' : 'disabled');
  }
  if (settings.webrtc_mode === 0 || settings.webrtc_mode === 1) {
    setRadioValue('webrtc_mode', settings.webrtc_mode === 1 ? 'enabled' : 'disabled');
  }
}

export function syncCameraView() {
  if (shared.dashboard) {
    applySettings(shared.dashboard.settings);
    renderGpioStatus(shared.dashboard);
  }
  loadGpioPins();
}

export async function submitSettingForm(event) {
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