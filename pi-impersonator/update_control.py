"""Authenticated signed-update read/check/install control (WP-UI7; AC-15).

This is the host-testable policy/state layer behind the admin System view's
update card. It is stdlib-only and side-effect free on import, and it never
touches the network, the signing key, the manifest URL, or a privileged command
itself: every hardware action is an injected zero-argument callable.

Security boundaries
-------------------
* **Read** is a bounded projection of the root-written
  ``/data/prusa-cam/update-state.json`` (the HA update schema) plus the active
  release identity. It never returns the manifest/bundle URL, the signing key,
  the channel, or a command.
* **Check** only calls the injected ``check_fn``, which in production starts the
  existing report-only root unit ``prusa-updater.service`` through the fixed-verb
  helper (``privileged.check_update``). It never installs.
* **Install** only calls the injected ``install_fn``, which in production starts
  ``prusa-updater-install.service`` through the fixed-verb helper
  (``privileged.install_update``). It never accepts a URL, CA, key, channel,
  bundle, manifest, version, service name, or command from a caller.
* No browser value reaches either callable: the request body is validated to be
  empty by the HTTP core, and the callables take no arguments.

Duplicate prevention and bounded deadlines
------------------------------------------
:class:`UpdateManager` admits at most one check and one install at a time. Each
operation runs on a daemon thread so the single admin-core worker is never
blocked; a flag older than its deadline is treated as stale and can be replaced,
so a crashed worker cannot wedge the control forever.
"""
import logging
import os
import threading
import time

import updater_install

log = logging.getLogger('prusa-cam.update_control')

#: Root-written HA update-state document (read-only projection).
DEFAULT_UPDATE_STATE_PATH = updater_install.DEFAULT_UPDATE_STATE_PATH

#: Root-written last-check timestamp document.
DEFAULT_LAST_CHECK_PATH = updater_install.DEFAULT_LAST_CHECK_PATH

#: Root-owned updater configuration. Only its presence/emptiness is observed;
#: the URL value is never read out of the process, returned, or logged.
DEFAULT_UPDATER_CONFIG_PATH = '/etc/prusa-updater.conf'

#: The manifest-URL environment key written by the root updater config.
MANIFEST_URL_ENV = updater_install.MANIFEST_URL_ENV

#: Bounded staleness deadlines for the in-flight flags (seconds). An operation
#: still flagged after this long is assumed dead and a new one may start.
CHECK_DEADLINE_SECONDS = 15 * 60.0
INSTALL_DEADLINE_SECONDS = 60 * 60.0

#: Bound on any returned reason string.
MAX_REASON_CHARS = 200

#: Bound on the projected release summary.
MAX_SUMMARY_CHARS = 256

#: Bound on the updater-config read.
MAX_CONFIG_BYTES = 8 * 1024


def _bounded(value, limit=MAX_REASON_CHARS):
    """Return a printable, control-character-free, length-bounded string."""
    if isinstance(value, BaseException):
        value = str(value)
    if not isinstance(value, str):
        return ''
    cleaned = ''.join(ch for ch in value if ch.isprintable()).strip()
    return cleaned[:limit]


def update_source_configured(path=DEFAULT_UPDATER_CONFIG_PATH):
    """Return True when a non-empty manifest URL is configured for the updater.

    Reads only the root-owned ``/etc/prusa-updater.conf`` (world-readable
    ``0644``) and checks whether :data:`MANIFEST_URL_ENV` is set to a non-empty
    value. The URL itself is never returned, logged, or retained. Never raises.
    """
    if not isinstance(path, str) or not path:
        return False
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            text = handle.read(MAX_CONFIG_BYTES)
    except OSError:
        return False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        if key.strip() == MANIFEST_URL_ENV:
            value = value.strip().strip('"').strip("'")
            return bool(value)
    return False


def _read_last_check_epoch(path=DEFAULT_LAST_CHECK_PATH):
    """Return the persisted last-check epoch, or ``None`` when unavailable.

    Reuses the updater's own bounded public reader so the timestamp format has
    one implementation. Never raises.
    """
    try:
        return updater_install.read_last_check(path)
    except Exception:  # noqa: BLE001 - a corrupt file must never crash a read
        return None


def _installed_version(identity, state):
    """Return the authoritative installed version (active release first)."""
    release = identity.get('version') if isinstance(identity, dict) else ''
    if isinstance(release, str) and release:
        return release
    value = state.get('installed_version') if isinstance(state, dict) else ''
    return value if isinstance(value, str) else ''


def build_update_view(*, state=None, identity=None, now, last_check=None,
                      checking=False, installing=False,
                      source_configured=False):
    """Build the bounded, secret-free update view (AC-15/AC-17).

    ``state`` is the already-normalized HA update document (or ``None``);
    ``identity`` is the active release identity. The projection intentionally
    drops ``release_url`` (an update source) and any unknown key, so the browser
    never receives a manifest/bundle location, signing key, channel, or command.
    """
    state = state if isinstance(state, dict) else {}
    installed = _installed_version(identity, state)
    latest = _bounded(state.get('latest_version'), updater_install.updater.MAX_VERSION_LENGTH)
    in_progress = bool(state.get('in_progress'))

    if installing or in_progress:
        state_name = 'installing'
    elif checking:
        state_name = 'checking'
    elif installed and latest and installed == latest:
        state_name = 'up-to-date'
    elif installed and latest:
        state_name = 'update-available'
    else:
        state_name = 'unknown'

    return {
        'state': state_name,
        'installed_version': installed,
        'latest_version': latest,
        'release_summary': _bounded(state.get('release_summary'), MAX_SUMMARY_CHARS),
        'update_percentage': (
            state.get('update_percentage')
            if type(state.get('update_percentage')) is int else None
        ),
        'in_progress': in_progress,
        'checking': bool(checking),
        'installing': bool(installing),
        'last_check': float(last_check) if isinstance(last_check, (int, float)) else None,
        'source_configured': bool(source_configured),
        'observed_at': now,
        'fresh': True,
    }


class UpdateManager:
    """Read/check/install policy over injected fixed-action callables.

    ``check_fn``/``install_fn`` are zero-argument callables returning a truthy
    result on success (a :class:`privileged.PrivilegedResult` in production).
    They are invoked on daemon threads so a slow or service-restarting action
    never blocks the admin core worker.
    """

    def __init__(self, *, check_fn, install_fn, state_reader=None,
                 last_check_reader=None, identity_fn=None,
                 source_configured_fn=None, clock=time.time,
                 monotonic=time.monotonic,
                 check_deadline=CHECK_DEADLINE_SECONDS,
                 install_deadline=INSTALL_DEADLINE_SECONDS):
        self._check_fn = check_fn
        self._install_fn = install_fn
        self._state_reader = state_reader or updater_install.read_update_state
        self._last_check_reader = last_check_reader or _read_last_check_epoch
        self._identity_fn = identity_fn
        self._source_configured_fn = (
            source_configured_fn if source_configured_fn is not None
            else update_source_configured
        )
        self._clock = clock
        self._monotonic = monotonic
        self._check_deadline = float(check_deadline)
        self._install_deadline = float(install_deadline)

        self._lock = threading.Lock()
        self._checking = False
        self._installing = False
        self._check_started = 0.0
        self._install_started = 0.0
        self._check_reason = ''
        self._install_reason = ''
        self._worker = None
        self._closed = False

    # -- reads ---------------------------------------------------------- #

    def _identity(self):
        if self._identity_fn is None:
            return {}
        try:
            value = self._identity_fn()
        except Exception:  # noqa: BLE001 - identity must never crash a read
            return {}
        return value if isinstance(value, dict) else {}

    def state(self):
        """Return the bounded update view plus the in-flight flags; never raises."""
        with self._lock:
            now_mono = self._monotonic()
            checking = self._checking and (
                now_mono - self._check_started) < self._check_deadline
            installing = self._installing and (
                now_mono - self._install_started) < self._install_deadline
        try:
            state = self._state_reader()
        except Exception:  # noqa: BLE001 - a bad state file must not crash a read
            state = None
        try:
            last_check = self._last_check_reader()
        except Exception:  # noqa: BLE001
            last_check = None
        try:
            configured = bool(self._source_configured_fn())
        except Exception:  # noqa: BLE001
            configured = False
        view = build_update_view(
            state=state,
            identity=self._identity(),
            now=self._clock(),
            last_check=last_check,
            checking=checking,
            installing=installing,
            source_configured=configured,
        )
        with self._lock:
            if self._check_reason and not checking:
                view['check_reason'] = self._check_reason
            if self._install_reason and not installing:
                view['install_reason'] = self._install_reason
        return view

    # -- actions -------------------------------------------------------- #

    def check(self):
        """Start one report-only check; refuse a duplicate. Never installs."""
        with self._lock:
            if self._closed:
                return {'ok': False, 'busy': False,
                        'reason': 'update control is shutting down'}
            if self._checking and (
                    self._monotonic() - self._check_started) < self._check_deadline:
                return {'ok': False, 'busy': True,
                        'reason': 'an update check is already in progress'}
            self._checking = True
            self._check_started = self._monotonic()
            self._check_reason = ''
            thread = threading.Thread(
                target=self._run_check, name='update-check', daemon=True)
            self._worker = thread
        thread.start()
        return {'ok': True, 'started': True, 'checking': True}

    def install(self):
        """Start one fixed-privileged install; refuse a duplicate."""
        with self._lock:
            if self._closed:
                return {'ok': False, 'busy': False,
                        'reason': 'update control is shutting down'}
            if self._installing and (
                    self._monotonic() - self._install_started) < self._install_deadline:
                return {'ok': False, 'busy': True,
                        'reason': 'an update install is already in progress'}
            if self._checking and (
                    self._monotonic() - self._check_started) < self._check_deadline:
                return {'ok': False, 'busy': True,
                        'reason': 'an update check is in progress'}
            self._installing = True
            self._install_started = self._monotonic()
            self._install_reason = ''
            thread = threading.Thread(
                target=self._run_install, name='update-install', daemon=True)
            self._worker = thread
        thread.start()
        return {'ok': True, 'started': True, 'installing': True}

    def _run_check(self):
        reason = ''
        try:
            result = self._check_fn()
            if not _truthy(result):
                reason = _bounded(getattr(result, 'reason', '') or 'update check failed')
        except Exception as e:  # noqa: BLE001 - a control action must never raise
            log.warning('update_control: check failed: %s', type(e).__name__)
            reason = 'update check failed'
        with self._lock:
            self._checking = False
            self._check_reason = reason

    def _run_install(self):
        reason = ''
        try:
            result = self._install_fn()
            if not _truthy(result):
                reason = _bounded(getattr(result, 'reason', '') or 'update install failed')
        except Exception as e:  # noqa: BLE001
            log.warning('update_control: install failed: %s', type(e).__name__)
            reason = 'update install failed'
        with self._lock:
            self._installing = False
            self._install_reason = reason

    def wait_for_idle(self, timeout=5.0):
        """Join the current worker thread (tests/shutdown); never raises."""
        with self._lock:
            thread = self._worker
        if thread is not None:
            try:
                thread.join(timeout=timeout)
            except Exception:  # noqa: BLE001
                pass

    def close(self):
        """Refuse new actions and best-effort join the current worker.

        Called from :meth:`admin_http.AdminApp.close` on transport shutdown. The
        join is bounded so a long install cannot block process exit; the worker
        threads are daemons and carry their own deadlines.
        """
        with self._lock:
            self._closed = True
        self.wait_for_idle(timeout=1.0)


def _truthy(result):
    """Return True when an injected action result reports success."""
    if result is None:
        return False
    if isinstance(result, bool):
        return result
    return bool(getattr(result, 'ok', False))


__all__ = [
    'CHECK_DEADLINE_SECONDS',
    'DEFAULT_LAST_CHECK_PATH',
    'DEFAULT_UPDATE_STATE_PATH',
    'DEFAULT_UPDATER_CONFIG_PATH',
    'INSTALL_DEADLINE_SECONDS',
    'MANIFEST_URL_ENV',
    'MAX_REASON_CHARS',
    'MAX_SUMMARY_CHARS',
    'UpdateManager',
    'build_update_view',
    'update_source_configured',
]
