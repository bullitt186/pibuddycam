/*
 * PiBuddyCam local console: Timelapse trigger form for the Prusa GPIO Hackerboard.
 *
 * ES module of the console (no bundler). Imports carry the content-hash query the
 * server substitutes for __ASSET_VERSION__, so a cached module can never be older
 * than the entry point that loads it.
 */

import { applySettings } from './camera.js?v=__ASSET_VERSION__';
import { SESSION_STATE, formatEpoch, handleExpired, isFormPending, request, setFormBusy, setFormStatus, setText, shared } from './common.js?v=__ASSET_VERSION__';


/* ------------------------------------------------------------------ */
/* Timelapse trigger: GPIO (Prusa GPIO Hackerboard)                    */
/* ------------------------------------------------------------------ */

/* Dwell suggested in the printer G-code before any latency was measured. */
const GPIO_DEFAULT_DWELL = 8;

const gpioUi = {
  pins: null,
  defaults: { shot: 17, record: 27 },
  loading: false,
  dirty: false,
  latency: null,
};

export function gpioEl(id) {
  return document.getElementById(id);
}

function fillPinSelect(select, includeNone) {
  if (!select || !gpioUi.pins) return;
  const previous = select.value;
  select.textContent = '';
  if (includeNone) {
    const none = document.createElement('option');
    none.value = '';
    none.textContent = 'None';
    select.appendChild(none);
  }
  gpioUi.pins.forEach((pin) => {
    const option = document.createElement('option');
    option.value = String(pin.bcm);
    option.textContent = pin.label;
    select.appendChild(option);
  });
  if (previous) select.value = previous;
}

export async function loadGpioPins() {
  if (gpioUi.pins || gpioUi.loading || !SESSION_STATE.csrf) return;
  gpioUi.loading = true;
  const result = await request('/api/gpio/pins');
  gpioUi.loading = false;
  if (result.status === 401) {
    handleExpired();
    return;
  }
  if (!result.ok || !result.data || !Array.isArray(result.data.pins)) return;
  gpioUi.pins = result.data.pins;
  if (result.data.defaults) gpioUi.defaults = result.data.defaults;
  fillPinSelect(gpioEl('timelapse-gpio-pin'), false);
  fillPinSelect(gpioEl('timelapse-gpio-record-pin'), true);
  if (shared.dashboard) {
    syncGpioForm(shared.dashboard.settings, true);
    renderGpioStatus(shared.dashboard);
  }
}

function gpioPinInfo(bcm) {
  if (!gpioUi.pins) return null;
  return gpioUi.pins.find((pin) => pin.bcm === Number(bcm)) || null;
}

function gpioSelectedPin(id) {
  const select = gpioEl(id);
  if (!select || select.value === '') return null;
  const value = Number(select.value);
  return Number.isInteger(value) ? value : null;
}

/** Show the pin fields only for the GPIO trigger and hide the timer field. */
function updateGpioVisibility() {
  const trigger = gpioEl('timelapse-trigger');
  const gpioMode = !!trigger && trigger.value === 'gpio';
  const fields = gpioEl('timelapse-gpio-fields');
  if (fields) fields.hidden = !gpioMode;
  const interval = gpioEl('timelapse-interval-form');
  if (interval) interval.hidden = gpioMode;
}

/** Keep the wiring and G-code panel in step with the selected pins. */
function updateGpioHelp() {
  const shot = gpioPinInfo(gpioSelectedPin('timelapse-gpio-pin') || gpioUi.defaults.shot);
  const recordBcm = gpioSelectedPin('timelapse-gpio-record-pin');
  const record = recordBcm == null ? null : gpioPinInfo(recordBcm);
  const set = (name, value) => {
    document.querySelectorAll(`[data-gpio="${name}"]`).forEach((node) => {
      node.textContent = String(value);
    });
  };
  if (shot) {
    set('shot-header', shot.header_pin);
    set('shot-bcm', shot.bcm);
    set('ground-header', shot.ground_pin);
  }
  if (record) {
    set('record-header', record.header_pin);
    set('record-bcm', record.bcm);
  }
  const wiring = gpioEl('gpio-help-record-wiring');
  if (wiring) wiring.hidden = !record;
  const start = gpioEl('gpio-help-record-start');
  if (start) start.hidden = !record;
  const measured = gpioUi.latency;
  const dwell = measured == null
    ? GPIO_DEFAULT_DWELL : Math.max(3, Math.ceil(measured) + 2);
  set('dwell', dwell);
  setText(
    gpioEl('gpio-help-dwell'),
    measured == null
      ? 'The dwell must be longer than the time from the pulse to the stored frame. No measurement yet: '
        + `${GPIO_DEFAULT_DWELL} s is a starting point. It is updated from the measured latency plus a margin once a frame has been captured.`
      : `The dwell must be longer than the time from the pulse to the stored frame. Measured: ${measured.toFixed(1)} s, so ${dwell} s leaves a margin of about 2 s.`,
  );
}

/** Fill the trigger form from the authoritative settings unless it is being edited. */
export function syncGpioForm(settings, force) {
  const form = gpioEl('timelapse-gpio-form');
  if (!form || !settings) return;
  const editing = form.contains(document.activeElement) || gpioUi.dirty;
  if (!force && (editing || isFormPending(form))) {
    updateGpioVisibility();
    return;
  }
  // The forced sync after the pin list loads must not undo a trigger the
  // user already switched while the pins were still loading.
  const trigger = gpioEl('timelapse-trigger');
  if (trigger && !gpioUi.dirty && (settings.timelapse_trigger === 'gpio'
      || settings.timelapse_trigger === 'interval')) {
    trigger.value = settings.timelapse_trigger;
  }
  if (gpioUi.pins) {
    const shot = gpioEl('timelapse-gpio-pin');
    const wanted = settings.timelapse_gpio_pin == null
      ? gpioUi.defaults.shot : settings.timelapse_gpio_pin;
    if (shot) shot.value = String(wanted);
    const record = gpioEl('timelapse-gpio-record-pin');
    if (record) {
      record.value = settings.timelapse_gpio_record_pin == null
        ? '' : String(settings.timelapse_gpio_record_pin);
    }
  }
  updateGpioVisibility();
  updateGpioHelp();
}

/** The live trigger status line: armed/error, last pulse, latency, recording. */
export function renderGpioStatus(data) {
  const line = gpioEl('timelapse-gpio-status');
  const errorBox = gpioEl('timelapse-gpio-error');
  if (!line) return;
  const settings = (data && data.settings) || {};
  const status = data && data.timelapse_gpio;
  if (!status || typeof status !== 'object') {
    setText(line, 'GPIO status is unavailable.');
    if (errorBox) errorBox.hidden = true;
    return;
  }
  gpioUi.latency = Number.isFinite(Number(status.latency_seconds))
    && status.latency_seconds != null ? Number(status.latency_seconds) : null;
  const parts = [];
  if (settings.timelapse_trigger !== 'gpio') {
    parts.push('The GPIO trigger is off (interval mode).');
  } else if (status.armed) {
    parts.push(`Armed on GPIO${status.shot_pin}`
      + (status.record_pin == null ? '' : `, recording on GPIO${status.record_pin}`));
  } else {
    parts.push('Not armed.');
  }
  if (status.last_trigger_at != null) {
    parts.push(`last pulse ${formatEpoch(status.last_trigger_at)}`);
  }
  if (gpioUi.latency != null) {
    parts.push(`pulse to frame ${gpioUi.latency.toFixed(1)} s`);
  }
  if (status.record_pin != null && status.armed) {
    parts.push(status.recording
      ? `recording ${status.session || ''}`.trim() : 'recording pin idle');
  }
  setText(line, parts.join(' · '));
  if (errorBox) {
    errorBox.textContent = status.error ? `GPIO problem: ${status.error}` : '';
    errorBox.hidden = !status.error;
  }
  updateGpioHelp();
}

async function patchSetting(field, value) {
  return request('/api/settings', {
    method: 'PATCH', csrf: true, body: { field, value },
  });
}

/**
 * Save the trigger. The coordinator refuses a GPIO trigger without a layer pin
 * and the same pin twice, so the writes are ordered to never pass through an
 * invalid state: switch to the timer first, clear the recording pin before
 * moving the layer pin, and enable the GPIO trigger last.
 */
async function submitGpioForm(event) {
  event.preventDefault();
  const form = gpioEl('timelapse-gpio-form');
  if (!form || isFormPending(form)) return;
  const trigger = gpioEl('timelapse-trigger').value;
  const shot = gpioSelectedPin('timelapse-gpio-pin');
  const record = gpioSelectedPin('timelapse-gpio-record-pin');
  if (trigger === 'gpio') {
    if (shot == null) {
      setFormStatus(form, 'error', 'Choose the layer pin first.');
      return;
    }
    if (record != null && record === shot) {
      setFormStatus(form, 'error', 'The layer pin and the recording pin must differ.');
      return;
    }
  }
  const current = (shared.dashboard && shared.dashboard.settings) || {};
  const steps = [];
  if (trigger === 'interval') {
    if (current.timelapse_trigger !== 'interval') steps.push(['timelapse_trigger', 'interval']);
    if (shot != null && shot !== current.timelapse_gpio_pin && shot !== record) {
      // Remember the choice for later; harmless while the timer is active.
      if (record == null || record !== current.timelapse_gpio_pin) {
        steps.push(['timelapse_gpio_pin', shot]);
      }
    }
  } else {
    if (current.timelapse_gpio_record_pin != null
        && (record !== current.timelapse_gpio_record_pin
            || current.timelapse_gpio_record_pin === shot)) {
      steps.push(['timelapse_gpio_record_pin', null]);
    }
    if (shot !== current.timelapse_gpio_pin) steps.push(['timelapse_gpio_pin', shot]);
    if (record != null && record !== current.timelapse_gpio_record_pin) {
      steps.push(['timelapse_gpio_record_pin', record]);
    } else if (record != null && current.timelapse_gpio_record_pin == null) {
      steps.push(['timelapse_gpio_record_pin', record]);
    }
    if (current.timelapse_trigger !== 'gpio') steps.push(['timelapse_trigger', 'gpio']);
  }
  if (steps.length === 0) {
    gpioUi.dirty = false;
    setFormStatus(form, 'ok', 'Nothing to change.');
    return;
  }
  setFormBusy(form, true);
  setFormStatus(form, 'info', 'Applying…');
  let settings = null;
  for (const [field, value] of steps) {
    const result = await patchSetting(field, value);
    if (result.status === 401) {
      setFormBusy(form, false);
      handleExpired();
      return;
    }
    const data = result.data || null;
    if (data && data.settings) settings = data.settings;
    if (result.status === 503) {
      setFormBusy(form, false);
      setFormStatus(form, 'error', 'The camera runtime is unavailable. Try again when it is running.');
      return;
    }
    if (!(result.ok && data && data.ok)) {
      setFormBusy(form, false);
      gpioUi.dirty = false;
      if (settings) applySettings(settings);
      setFormStatus(form, 'error', (data && data.error) || 'The change was rejected.');
      return;
    }
  }
  setFormBusy(form, false);
  gpioUi.dirty = false;
  if (settings) {
    if (shared.dashboard) shared.dashboard.settings = settings;
    applySettings(settings);
  }
  setFormStatus(form, 'ok', 'Saved.');
}

export function wireGpio() {
  const form = gpioEl('timelapse-gpio-form');
  if (!form) return;
  form.addEventListener('submit', submitGpioForm);
  form.addEventListener('change', () => {
    gpioUi.dirty = true;
    updateGpioVisibility();
    updateGpioHelp();
  });
}