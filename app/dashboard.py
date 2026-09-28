"""Authoritative dashboard aggregation for the local Web UI (WP-UI2; AC-14/AC-17).

The camera runtime is the single authoritative source of settings and runtime
state. This module turns the live runtime inputs into one bounded, redacted
JSON document that the admin ``GET /api/dashboard`` route serves. It is
stdlib-only and side-effect free on import, so the whole payload is host
testable without a Pi, ``aiohttp``, or a live socket.

Freshness model (AC-14)
-----------------------
Every subsystem reports three separate things:

* the **configured** mode (what the durable settings say),
* the **runtime state** (what the process actually observes), and
* **freshness** (``observed_at``/``age_seconds``/``fresh``) so the UI never
  infers "online" from configuration.

An observation that was never made is reported as ``unknown`` with
``observed_at: null`` rather than a fabricated value. Free-form text is
bounded, personal home paths are replaced with ``<path>``, tracebacks collapse
to ``error``, and the whole document is passed through
:func:`admin_auth.redact` as defence in depth.

The module also owns :class:`DashboardProvider`, a tiny cache that refreshes the
runtime payload on a background daemon thread. The admin core worker reads the
cache, so a slow or absent runtime can never serialize long operations through
the single admin-core executor.
"""
import logging
import re
import threading
import time

import admin_auth
import mqtt_state
from runtime_ipc import bound_value, bounded_text

log = logging.getLogger('pibuddycam.dashboard')

#: Per-subsystem freshness thresholds (seconds). An observation older than its
#: threshold is reported with ``fresh: false`` but is not hidden.
FRESHNESS_SECONDS = {
    'camera': 120.0,
    'prusa': 60.0,
    'snapshots': 120.0,
    'rtsp': 60.0,
    'webrtc': 60.0,
    'mqtt': 120.0,
    'storage': 60.0,
    'metrics': 60.0,
    'updates': 3600.0,
}

#: The only authoritative settings keys the dashboard exposes.
SETTINGS_KEYS = (
    'quality_tier',
    'camera_name',
    'snapshot_interval',
    'snapshot_upload_enabled',
    'timelapse_interval',
    'timelapse_enabled',
    'timelapse_fps',
    'timelapse_trigger',
    'timelapse_gpio_pin',
    'timelapse_gpio_record_pin',
    'rtsp_mode',
    'webrtc_mode',
)

#: Personal home-directory path fragment: never publish a real username path.
_PERSONAL_PATH_RE = re.compile(r'(?i)(?:/home/|/users/|/root/)[^\s\'"]*')

#: Longest free-form text kept in any field.
MAX_FIELD_CHARS = 512


def _safe_text(value, limit=MAX_FIELD_CHARS):
    """Return bounded, printable, path- and traceback-free text."""
    text = bounded_text(value, limit)
    if not text:
        return ''
    if 'Traceback' in text or 'File "' in text:
        return 'error'
    return _PERSONAL_PATH_RE.sub('<path>', text)


def _sanitize(value):
    """Recursively sanitize every string in ``value`` (keys are fixed)."""
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _read(source, name, default=None):
    """Read ``name`` from a mapping or object; never raises."""
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _observation(state, *, configured=None, observed_at=None, now=0.0,
                 threshold=60.0):
    """Build one configured/state/freshness triple (AC-14)."""
    result = {'state': state}
    if configured is not None:
        result['configured'] = configured
    if observed_at is None:
        result['observed_at'] = None
        result['age_seconds'] = None
        result['fresh'] = False
    else:
        try:
            age = max(0.0, float(now) - float(observed_at))
        except (TypeError, ValueError):
            age = None
        if age is None:
            result['observed_at'] = None
            result['age_seconds'] = None
            result['fresh'] = False
        else:
            result['observed_at'] = observed_at
            result['age_seconds'] = round(age, 3)
            result['fresh'] = age <= threshold
    return result


def _settings_view(settings):
    """Project the authoritative settings onto the allowlisted key set."""
    if not isinstance(settings, dict):
        return {}
    return bound_value({key: settings[key] for key in SETTINGS_KEYS if key in settings})


def _version_view(application_version, build_identity):
    identity = build_identity if isinstance(build_identity, dict) else {}
    return {
        'application': _safe_text(application_version, 128),
        'release': _safe_text(identity.get('version'), 128),
        'source_commit': _safe_text(identity.get('source_commit'), 64),
        'os_suite': _safe_text(identity.get('os_suite'), 64),
        'kernel': _safe_text(identity.get('kernel_package'), 64),
    }


def _resolution_view(state):
    try:
        # Delivered size: swapped for a 90/270-degree rotation.
        resolve = getattr(state, 'oriented_resolution', None) or state.resolution
        width, height = resolve()
    except Exception:  # noqa: BLE001 - a broken state object must not crash
        return {'width': None, 'height': None, 'label': 'unknown'}
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        return {'width': None, 'height': None, 'label': 'unknown'}
    return {'width': width, 'height': height, 'label': f'{width}x{height}'}


def _quality_view(state):
    tier = _read(state, 'quality', None)
    try:
        name = mqtt_state.quality_name(tier)
    except (ValueError, TypeError):
        name = 'unknown'
    return {'tier': tier if type(tier) is int else None, 'name': name}


def _prusa_view(signaling, now):
    if signaling is None:
        return _observation('unknown', configured=True, observed_at=None)
    authenticated = _read(signaling, 'authenticated', None)
    connected = _read(signaling, 'connected', None)
    if connected is False:
        signaling_state = 'disconnected'
    elif connected is True:
        signaling_state = 'connected'
    else:
        signaling_state = 'unknown'
    if authenticated is True:
        auth_state = 'authenticated'
    elif authenticated is False:
        auth_state = 'unauthenticated'
    else:
        auth_state = 'unknown'
    if authenticated is True:
        overall = 'authenticated'
    elif connected is True:
        overall = 'connected'
    else:
        overall = signaling_state
    observed = now if (connected is not None or authenticated is not None) else None
    view = _observation(overall, configured=True, observed_at=observed, now=now,
                        threshold=FRESHNESS_SECONDS['prusa'])
    view['signaling'] = signaling_state
    view['auth'] = auth_state
    return view


def _mqtt_view(mqtt, now):
    if mqtt is None:
        return _observation('unknown', configured=False, observed_at=None)
    if isinstance(mqtt, dict):
        enabled = bool(mqtt.get('enabled', False))
        started = mqtt.get('started')
        last_error = mqtt.get('last_error')
    else:
        config = getattr(mqtt, 'config', None)
        enabled = bool(getattr(config, 'enabled', False)) if config is not None else False
        started = getattr(mqtt, 'started', None)
        last_error = getattr(mqtt, 'last_command_error', None)
    if started is True:
        state_name = 'connected'
    elif started is False and enabled:
        state_name = 'unknown'
    elif started is False:
        state_name = 'disconnected'
    else:
        state_name = 'unknown'
    view = _observation(state_name, configured=enabled,
                        observed_at=now if enabled else None, now=now,
                        threshold=FRESHNESS_SECONDS['mqtt'])
    view['last_error'] = mqtt_state.sanitize_command_error(last_error)
    return view


def _updates_view(updates, now):
    if not isinstance(updates, dict):
        return _observation('unknown', configured=False, observed_at=None)
    installed = _safe_text(updates.get('installed_version'), 64)
    latest = _safe_text(updates.get('latest_version'), 64)
    in_progress = bool(updates.get('in_progress'))
    if in_progress:
        state_name = 'installing'
    elif installed and latest and installed == latest:
        state_name = 'up-to-date'
    elif installed and latest:
        state_name = 'update-available'
    else:
        state_name = 'unknown'
    view = _observation(state_name, configured=True, observed_at=now, now=now,
                        threshold=FRESHNESS_SECONDS['updates'])
    view['installed_version'] = installed
    view['latest_version'] = latest
    view['in_progress'] = in_progress
    view['release_summary'] = _safe_text(updates.get('release_summary'), 256)
    return view


def build_dashboard(*, now, settings=None, state=None, metrics=None,
                    application_version='', build_identity=None,
                    signaling=None, mqtt=None, updates=None, snapshot=None,
                    runtime_available=True, reason='', secrets=(), gpio=None):
    """Build the bounded, redacted dashboard document.

    All inputs are injected so the function is pure and host testable. ``state``
    may be a :class:`state.CameraState` or any object/mapping with the same
    attribute names; only the documented attributes are read.
    """
    metrics = metrics if isinstance(metrics, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}

    payload = {
        'ok': True,
        'generated_at': now,
        'runtime': {
            'source': 'live' if runtime_available else 'unavailable',
            'observed_at': now if runtime_available else None,
            'age_seconds': 0.0 if runtime_available else None,
            'fresh': bool(runtime_available),
        },
        'version': _version_view(application_version, build_identity),
        'settings': _settings_view(settings),
    }
    if reason:
        payload['runtime']['reason'] = _safe_text(reason, 200)

    # -- camera source (configured quality/resolution vs observed liveness) --
    capture_at = snapshot.get('capture_at')
    capture_ok = snapshot.get('capture_ok')
    streaming = bool(_read(state, 'streaming', False))
    if streaming or capture_ok is True:
        camera_state = 'running'
    elif capture_ok is False:
        camera_state = 'error'
    else:
        camera_state = 'unknown'
    camera = _observation(camera_state, configured=True, observed_at=capture_at,
                          now=now, threshold=FRESHNESS_SECONDS['camera'])
    camera['quality'] = _quality_view(state)
    camera['resolution'] = _resolution_view(state)
    payload['camera'] = camera

    # -- Prusa signaling / auth --
    payload['prusa'] = _prusa_view(signaling, now)

    # -- periodic snapshots --
    snapshot_at = snapshot.get('last_at')
    snapshot_ok = snapshot.get('ok')
    interval = _read(state, 'snapshot_interval', None)
    if snapshot_ok is True:
        snapshot_state = 'ok'
    elif snapshot_ok is False:
        snapshot_state = 'failed'
    else:
        snapshot_state = 'unknown'
    snapshot_threshold = max(FRESHNESS_SECONDS['snapshots'],
                             2.0 * float(interval or 0))
    snapshots = _observation(
        snapshot_state,
        configured=bool(_read(state, 'snapshot_upload_enabled', False)),
        observed_at=snapshot_at, now=now, threshold=snapshot_threshold)
    snapshots['interval_seconds'] = interval if type(interval) is int else None
    snapshots['last_at'] = snapshot_at
    payload['snapshots'] = snapshots

    # -- RTSP (configured mode vs actual service state) --
    rtsp_mode = _read(state, 'rtsp_mode', None)
    rtsp_configured = (
        'enabled' if rtsp_mode == 2 else ('disabled' if rtsp_mode == 1 else 'unknown'))
    rtsp_running = _read(state, 'rtsp_running', None)
    rtsp_state = (
        'running' if rtsp_running is True
        else ('stopped' if rtsp_running is False else 'unknown'))
    payload['rtsp'] = _observation(
        rtsp_state, configured=rtsp_configured, observed_at=now, now=now,
        threshold=FRESHNESS_SECONDS['rtsp'])

    # -- WebRTC (configured mode vs streaming state) --
    webrtc_mode = _read(state, 'webrtc_mode', None)
    webrtc_configured = (
        'enabled' if webrtc_mode == 1 else ('disabled' if webrtc_mode == 0 else 'unknown'))
    webrtc_status = _read(state, 'webrtc_status', None)
    if streaming:
        webrtc_state = 'streaming'
    elif webrtc_status == 1:
        webrtc_state = 'running'
    elif webrtc_status == 0:
        webrtc_state = 'stopped'
    else:
        webrtc_state = 'unknown'
    webrtc = _observation(
        webrtc_state, configured=webrtc_configured, observed_at=now, now=now,
        threshold=FRESHNESS_SECONDS['webrtc'])
    webrtc['streaming'] = streaming
    webrtc['turn_online'] = bool(_read(state, 'turn_online', False))
    payload['webrtc'] = webrtc

    # -- MQTT --
    payload['mqtt'] = _mqtt_view(mqtt, now)

    # -- storage + metrics --
    free_bytes = metrics.get('storage_free_bytes')
    storage = _observation(
        'available' if free_bytes is not None else 'unknown',
        observed_at=now if free_bytes is not None else None, now=now,
        threshold=FRESHNESS_SECONDS['storage'])
    storage['free_bytes'] = free_bytes if type(free_bytes) is int else None
    payload['storage'] = storage

    metric_values = {
        'wifi_rssi_dbm': metrics.get('wifi_rssi_dbm'),
        'cpu_temperature_c': metrics.get('cpu_temperature_c'),
        'uptime_seconds': metrics.get('uptime_seconds'),
    }
    metric_observed = now if any(
        value is not None for value in metric_values.values()) else None
    metric_view = _observation(
        'available' if metric_observed is not None else 'unknown',
        observed_at=metric_observed, now=now, threshold=FRESHNESS_SECONDS['metrics'])
    metric_view.update(metric_values)
    payload['metrics'] = metric_view

    # -- signed updates --
    payload['updates'] = _updates_view(updates, now)

    # -- Pi-only timelapse GPIO trigger status (armed/error, latency, session) --
    if isinstance(gpio, dict):
        payload['timelapse_gpio'] = gpio

    payload = _sanitize(payload)
    return admin_auth.redact(payload, secrets)


def unavailable_dashboard(now, reason='runtime control unavailable'):
    """Return the honest degraded document used when the runtime is absent."""
    return build_dashboard(
        now=now, runtime_available=False,
        reason=reason or 'runtime control unavailable')


class DashboardProvider:
    """Cache the runtime dashboard payload on a background daemon thread.

    The admin core worker calls :meth:`__call__` (or :meth:`snapshot`) and gets
    the last successful payload instantly. A missing runtime yields the degraded
    document; a later failure keeps the last-known payload but marks it
    ``stale`` with the failure reason, so the UI never claims live data.
    """

    def __init__(self, client, *, refresh_interval=5.0, stale_after=20.0,
                 wall_clock=time.time):
        self._client = client
        self._refresh_interval = max(0.5, float(refresh_interval))
        self._stale_after = max(self._refresh_interval, float(stale_after))
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._payload = None
        self._received_at = None
        self._last_error = ''
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        """Start the background refresher (idempotent)."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name='dashboard-cache', daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the background refresher; never raises."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)

    def refresh(self):
        """Fetch one payload from the runtime and cache it; never raises."""
        try:
            result = self._client.dashboard()
        except Exception:  # noqa: BLE001 - a client must never crash the provider
            result = None
        now = self._wall_clock()
        if isinstance(result, dict) and result.get('ok') and isinstance(result.get('data'), dict):
            with self._lock:
                self._payload = result['data']
                self._received_at = now
                self._last_error = ''
            return self._payload
        reason = ''
        if isinstance(result, dict):
            reason = bounded_text(result.get('error'), 200)
        with self._lock:
            self._last_error = reason or 'runtime unavailable'
        return None

    def _run(self):
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self._refresh_interval)

    def snapshot(self):
        """Return the last-known payload stamped with freshness; never raises."""
        with self._lock:
            payload, received, error = self._payload, self._received_at, self._last_error
        if payload is None:
            self.refresh()
            with self._lock:
                payload, received, error = self._payload, self._received_at, self._last_error
        now = self._wall_clock()
        if payload is None:
            return unavailable_dashboard(now, reason=error or 'runtime unavailable')
        stamped = dict(payload)
        runtime = dict(stamped.get('runtime') or {})
        age = max(0.0, now - received) if received is not None else None
        fresh = age is not None and age <= self._stale_after
        runtime['fetched_at'] = received
        runtime['age_seconds'] = round(age, 3) if age is not None else None
        runtime['fresh'] = bool(fresh)
        if fresh:
            runtime['source'] = 'live'
        else:
            runtime['source'] = 'stale'
            if error:
                runtime['reason'] = _safe_text(error, 200)
        stamped['runtime'] = runtime
        return stamped

    def __call__(self):
        if self._thread is None:
            # Lazily start on first request so importing/building the admin app
            # has no thread side effect.
            self.start()
        return self.snapshot()


__all__ = [
    'DashboardProvider',
    'FRESHNESS_SECONDS',
    'SETTINGS_KEYS',
    'build_dashboard',
    'unavailable_dashboard',
]
