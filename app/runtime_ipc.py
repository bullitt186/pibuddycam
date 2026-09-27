"""Bounded local runtime-control IPC (WP-UI2; plan AC-4/AC-17/AC-18).

The admin UI (``pibuddycam-admin``) and the camera runtime (``pibuddycam``) are
separate processes, so the admin must never construct a second ``CameraState``
or ``SettingsCoordinator``. This module is the single, bounded control boundary
between them:

* an ``AF_UNIX`` stream socket under ``/run/pibuddycam/`` owned by the service
  account with an explicit mode;
* newline-delimited JSON framing with a hard request/response size cap;
* a fixed operation allowlist (read/status only in this slice);
* per-connection deadlines so a malformed or slow peer can never starve the
  other side; and
* total exception isolation: neither side raises on bad input, an unavailable
  socket, or a failing handler.

The runtime owns the live ``SettingsCoordinator``; the admin calls read-only
operations through :class:`RuntimeClient`. A missing socket is reported as a
degraded result, never as an empty success. There is deliberately **no**
``state.json`` fallback: only the live runtime may mutate durable state.

Framing
-------
Request:  ``{"op": "<allowlisted-name>", "params": {...}}\\n``
Response: ``{"ok": true, "data": {...}}\\n`` or
          ``{"ok": false, "error": "<bounded reason>"}\\n``

Stdlib only, and no file/network/thread side effects on import.
"""
import concurrent.futures
import json
import logging
import os
import socket
import stat
import threading
import time

log = logging.getLogger('pibuddycam.runtime_ipc')

#: Runtime directory the service account owns (created by systemd
#: ``RuntimeDirectory=pibuddycam`` on ``pibuddycam.service``).
DEFAULT_RUNTIME_DIR = '/run/pibuddycam'

#: Socket basename inside the runtime directory.
DEFAULT_SOCKET_NAME = 'control.sock'

#: Default socket path (the runtime server and the admin client agree on this).
DEFAULT_SOCKET_PATH = os.path.join(DEFAULT_RUNTIME_DIR, DEFAULT_SOCKET_NAME)

#: Socket file mode: service-account only. The directory is service-owned too,
#: so another local user cannot pre-create or replace the socket.
DEFAULT_SOCKET_MODE = 0o600

#: Per-connection read/write deadline (seconds). Bounds a hung or slow peer.
DEFAULT_TIMEOUT_SECONDS = 3.0

#: Hard caps on one request line and one response line.
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024

#: Bounds applied to request params and response data before serialization.
MAX_PARAMS_DEPTH = 4
MAX_PARAMS_ITEMS = 64
MAX_RESPONSE_DEPTH = 8
MAX_RESPONSE_ITEMS = 256
MAX_TEXT_CHARS = 4096
MAX_KEY_CHARS = 128

#: The read/status operations this slice exposes. A name not in this tuple can
#: never be dispatched, so a future write slice must extend it explicitly.
READ_OPERATIONS = ('dashboard', 'ping')

#: WP-UI3/AC-5: the only mutating operation the runtime exposes. It carries one
#: allowlisted settings field/value pair; the runtime's ``SettingsCoordinator``
#: performs the actual validation and apply. A future mutation must be added
#: here explicitly -- there is no generic "set attribute" operation.
WRITE_OPERATIONS = ('settings.set',)

#: Every operation the server may dispatch and the client may request.
ALLOWED_OPERATIONS = READ_OPERATIONS + WRITE_OPERATIONS

#: Connection admission bounds: at most ``max_connections`` in flight and
#: ``max_workers`` being served at once. Excess connections are closed at once
#: rather than queued, so a flood cannot grow memory.
DEFAULT_MAX_WORKERS = 2
DEFAULT_MAX_CONNECTIONS = 4
DEFAULT_BACKLOG = 8


def bounded_text(value, limit=MAX_TEXT_CHARS):
    """Return ``value`` as a printable, length-bounded string (or ``''``)."""
    if not isinstance(value, str):
        return ''
    if not value:
        return ''
    cleaned = ''.join(ch for ch in value if ch.isprintable())
    return cleaned[:limit]


def bound_value(value, *, depth=MAX_PARAMS_DEPTH, max_items=MAX_PARAMS_ITEMS,
                max_text=MAX_TEXT_CHARS):
    """Return a depth/size-bounded, JSON-safe copy of ``value``.

    Unknown objects are converted to a bounded string, so an accidental secret
    object or a huge nested structure cannot be serialized whole. This is the
    single bound applied to every IPC request and response value.
    """
    if depth < 0:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return bounded_text(value, max_text)
    if isinstance(value, (list, tuple)):
        return [
            bound_value(item, depth=depth - 1, max_items=max_items, max_text=max_text)
            for item in list(value)[:max_items]
        ]
    if isinstance(value, dict):
        bounded = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                break
            bounded[bounded_text(str(key), MAX_KEY_CHARS)] = bound_value(
                item, depth=depth - 1, max_items=max_items, max_text=max_text)
        return bounded
    return bounded_text(str(value), max_text)


class RuntimeServer:
    """Serve allowlisted read operations over a bounded Unix socket.

    The server runs on its own daemon thread plus a small worker pool, so the
    runtime's asyncio event loop is never touched: a slow metric probe or a
    malformed client cannot stall Prusa signaling. Call :meth:`start` once and
    :meth:`stop` on shutdown.
    """

    def __init__(self, socket_path=DEFAULT_SOCKET_PATH, handlers=None, *,
                 socket_mode=DEFAULT_SOCKET_MODE,
                 max_request_bytes=MAX_REQUEST_BYTES,
                 max_response_bytes=MAX_RESPONSE_BYTES,
                 timeout=DEFAULT_TIMEOUT_SECONDS,
                 backlog=DEFAULT_BACKLOG,
                 max_workers=DEFAULT_MAX_WORKERS,
                 max_connections=DEFAULT_MAX_CONNECTIONS):
        if not isinstance(socket_path, str) or not socket_path:
            raise ValueError('socket_path must be a non-empty string')
        if socket_mode not in range(0o400, 0o1000):
            raise ValueError('socket_mode must be an explicit file mode')
        self._socket_path = socket_path
        self._socket_mode = socket_mode
        self._max_request_bytes = int(max_request_bytes)
        self._max_response_bytes = int(max_response_bytes)
        self._timeout = float(timeout)
        self._backlog = int(backlog)
        self._max_workers = max(1, int(max_workers))
        self._max_connections = max(1, int(max_connections))

        # The handler table *is* the operation allowlist: an unknown name can
        # never reach a callable. ``ping`` is a built-in liveness probe.
        self._handlers = {}
        for name, handler in (handlers or {}).items():
            if isinstance(name, str) and name in ALLOWED_OPERATIONS and callable(handler):
                self._handlers[name] = handler
        self._handlers.setdefault('ping', lambda params: {'pong': True})

        self._listener = None
        self._accept_thread = None
        self._pool = None
        self._slots = threading.BoundedSemaphore(self._max_connections)
        self._stop = threading.Event()
        self._started = False

    @property
    def socket_path(self):
        return self._socket_path

    @property
    def started(self):
        return self._started

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        """Bind, listen, and start serving; idempotent. Raises on bind failure."""
        if self._started:
            return
        self._prepare_socket()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(self._socket_path)
            os.chmod(self._socket_path, self._socket_mode)
            listener.listen(self._backlog)
            listener.settimeout(0.25)
        except Exception:
            _close_socket(listener)
            raise
        self._listener = listener
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix='runtime-ipc')
        self._stop.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name='runtime-ipc-accept', daemon=True)
        self._accept_thread.start()
        self._started = True
        log.info('runtime_ipc: serving on %s', self._socket_path)

    def stop(self):
        """Stop serving, close the listener, and unlink the socket; never raises."""
        self._stop.set()
        listener, self._listener = self._listener, None
        _close_socket(listener)
        thread, self._accept_thread = self._accept_thread, None
        if thread is not None:
            thread.join(timeout=1.0)
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:  # pragma: no cover - older Python without cancel_futures
                pool.shutdown(wait=False)
        self._unlink_socket()
        self._started = False
        log.info('runtime_ipc: stopped %s', self._socket_path)

    def _prepare_socket(self):
        directory = os.path.dirname(self._socket_path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        if not os.path.lexists(self._socket_path):
            return
        try:
            existing = os.lstat(self._socket_path)
        except OSError:
            return
        if stat.S_ISSOCK(existing.st_mode):
            # A stale socket from a previous run is safe to replace.
            os.unlink(self._socket_path)
            return
        raise OSError(
            f'refusing to replace non-socket path {self._socket_path}')

    def _unlink_socket(self):
        try:
            existing = os.lstat(self._socket_path)
        except OSError:
            return
        if not stat.S_ISSOCK(existing.st_mode):
            return
        try:
            os.unlink(self._socket_path)
        except OSError:
            log.debug('runtime_ipc: could not unlink %s', self._socket_path)

    # -- serving -----------------------------------------------------------

    def _accept_loop(self):
        listener = self._listener
        while not self._stop.is_set() and listener is not None:
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._slots.acquire(blocking=False):
                # At capacity: drop the peer immediately rather than queue it.
                _close_socket(connection)
                continue
            pool = self._pool
            if pool is None:
                self._slots.release()
                _close_socket(connection)
                continue
            try:
                pool.submit(self._serve_connection, connection)
            except RuntimeError:
                self._slots.release()
                _close_socket(connection)

    def _serve_connection(self, connection):
        try:
            connection.settimeout(self._timeout)
            line = self._read_line(connection, self._max_request_bytes)
            if line is None:
                return
            response = self._dispatch(line)
            payload = json.dumps(response, sort_keys=True, default=str).encode('utf-8')
            if len(payload) > self._max_response_bytes:
                payload = json.dumps(
                    {'ok': False, 'error': 'response too large'}, sort_keys=True
                ).encode('utf-8')
            connection.sendall(payload + b'\n')
        except Exception:  # noqa: BLE001 - one bad connection must not kill the server
            log.debug('runtime_ipc: connection handling failed', exc_info=True)
        finally:
            _close_socket(connection)
            try:
                self._slots.release()
            except ValueError:  # pragma: no cover - released twice
                pass

    def _read_line(self, connection, limit):
        """Read one newline-terminated line bounded by ``limit`` and a deadline."""
        deadline = time.monotonic() + self._timeout
        chunks = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            connection.settimeout(remaining)
            try:
                chunk = connection.recv(4096)
            except (socket.timeout, TimeoutError, OSError):
                return None
            if not chunk:
                return None
            chunks.extend(chunk)
            if len(chunks) > limit:
                return None
            if b'\n' in chunk:
                break
        line, _, _ = bytes(chunks).partition(b'\n')
        return line or None

    def _dispatch(self, line):
        """Parse and route one request line; never raises."""
        try:
            request = json.loads(line.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return {'ok': False, 'error': 'malformed request'}
        if not isinstance(request, dict):
            return {'ok': False, 'error': 'malformed request'}
        op = request.get('op')
        if not isinstance(op, str) or op not in self._handlers:
            return {'ok': False, 'error': 'operation not allowed'}
        params = request.get('params')
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return {'ok': False, 'error': 'invalid params'}
        try:
            data = self._handlers[op](bound_value(params))
        except Exception:  # noqa: BLE001 - a handler must never break the server
            log.warning('runtime_ipc: operation %s failed', op)
            return {'ok': False, 'error': 'operation failed'}
        return {'ok': True, 'data': bound_value(
            data, depth=MAX_RESPONSE_DEPTH, max_items=MAX_RESPONSE_ITEMS)}


class RuntimeClient:
    """Blocking, bounded client for the runtime control socket.

    Every method returns a plain dict and never raises: an absent socket, a
    timeout, a malformed response, or a remote error becomes a degraded result
    with ``ok: False``. The caller (the admin provider) turns that into the
    dashboard's degraded state.
    """

    def __init__(self, socket_path=DEFAULT_SOCKET_PATH, *,
                 timeout=DEFAULT_TIMEOUT_SECONDS,
                 max_request_bytes=MAX_REQUEST_BYTES,
                 max_response_bytes=MAX_RESPONSE_BYTES):
        if not isinstance(socket_path, str) or not socket_path:
            raise ValueError('socket_path must be a non-empty string')
        self._socket_path = socket_path
        self._timeout = float(timeout)
        self._max_request_bytes = int(max_request_bytes)
        self._max_response_bytes = int(max_response_bytes)

    @property
    def socket_path(self):
        return self._socket_path

    def request(self, op, params=None):
        """Send one allowlisted operation and return a bounded result dict."""
        if not isinstance(op, str) or op not in ALLOWED_OPERATIONS:
            return self._degraded('operation not allowed')
        envelope = {'op': op, 'params': bound_value(params or {})}
        try:
            payload = json.dumps(envelope, sort_keys=True, default=str).encode('utf-8')
        except (TypeError, ValueError):
            return self._degraded('invalid request')
        if len(payload) > self._max_request_bytes:
            return self._degraded('request too large')

        connection = None
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(self._timeout)
            connection.connect(self._socket_path)
            connection.sendall(payload + b'\n')
            line = self._read_line(connection, self._max_response_bytes)
            if line is None:
                return self._degraded('runtime unavailable')
            response = json.loads(line.decode('utf-8'))
        except (OSError, ValueError, UnicodeDecodeError):
            return self._degraded('runtime unavailable')
        finally:
            _close_socket(connection)

        if not isinstance(response, dict) or not response.get('ok'):
            reason = ''
            if isinstance(response, dict):
                reason = bounded_text(response.get('error'), 200)
            return self._degraded(reason or 'runtime error')
        return {
            'ok': True,
            'data': bound_value(
                response.get('data'), depth=MAX_RESPONSE_DEPTH,
                max_items=MAX_RESPONSE_ITEMS),
        }

    def dashboard(self):
        """Return the runtime's authoritative dashboard payload (or degraded)."""
        return self.request('dashboard')

    def ping(self):
        """Return a liveness probe result (or degraded)."""
        return self.request('ping')

    def settings_set(self, field, value):
        """Apply one allowlisted settings mutation through the live coordinator.

        Returns the runtime's bounded ``{ok, data}`` envelope (or a degraded
        result when the socket is absent). The field/value are bounded and the
        runtime validates them again; a bad value is a normal rejection, not a
        transport error.
        """
        return self.request('settings.set', {'field': field, 'value': value})

    def _read_line(self, connection, limit):
        deadline = time.monotonic() + self._timeout
        chunks = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            connection.settimeout(remaining)
            try:
                chunk = connection.recv(4096)
            except (socket.timeout, TimeoutError, OSError):
                return None
            if not chunk:
                return None
            chunks.extend(chunk)
            if len(chunks) > limit:
                return None
            if b'\n' in chunk:
                break
        line, _, _ = bytes(chunks).partition(b'\n')
        return line or None

    @staticmethod
    def _degraded(reason):
        return {
            'ok': False,
            'error': bounded_text(reason, 200) or 'runtime unavailable',
            'degraded': True,
        }


def _close_socket(sock):
    """Close ``sock`` best-effort; never raises."""
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


__all__ = [
    'ALLOWED_OPERATIONS',
    'DEFAULT_MAX_CONNECTIONS',
    'DEFAULT_MAX_WORKERS',
    'DEFAULT_RUNTIME_DIR',
    'DEFAULT_SOCKET_MODE',
    'DEFAULT_SOCKET_NAME',
    'DEFAULT_SOCKET_PATH',
    'DEFAULT_TIMEOUT_SECONDS',
    'MAX_REQUEST_BYTES',
    'MAX_RESPONSE_BYTES',
    'READ_OPERATIONS',
    'RuntimeClient',
    'RuntimeServer',
    'WRITE_OPERATIONS',
    'bound_value',
    'bounded_text',
]
