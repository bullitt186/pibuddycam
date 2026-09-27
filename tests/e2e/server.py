#!/usr/bin/env python3
"""Deterministic local E2E harness for the admin Web console (WP-UI8; AC-18/AC-19).

This module serves the **real** stdlib admin core (:class:`admin_http.AdminApp`)
with the **real** packaged assets in ``app/web/`` over a small local
``http.server`` transport. Every dependency that would touch the host -- the
camera runtime, MQTT broker, updater, diagnostics, SSH, reboot, factory reset,
and the live-monitor producer -- is replaced by an in-process deterministic fake.

Properties this harness deliberately preserves:

* real session, CSRF, re-auth-window, rate-limit, redaction, static-asset, and
  route-policy behavior (all of it lives in ``admin_http`` and is exercised);
* synthetic-only state: a throwaway temp tree for ``device.toml`` /
  ``secrets.toml`` / ``provisioning.json`` / media, and a fixed synthetic admin
  password. No ``/data``, no network, no subprocess, no device, no secrets;
* a deterministic reset hook so one browser test cannot leak state into another.

The harness also exposes a small ``/__e2e/`` control surface used only by the
Playwright tests (reset state, flip a scenario, revoke sessions, read action
counters). It is not part of the product and is never served by ``admin_app``.
"""
from __future__ import annotations

import base64
import json
import signal
import struct
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
if str(PI_DIR) not in sys.path:
    sys.path.insert(0, str(PI_DIR))

import admin_auth  # noqa: E402
import admin_http  # noqa: E402
import config_schema  # noqa: E402
import provisioning  # noqa: E402

#: Fixed synthetic admin password. It is not a real credential and is never
#: reused outside this local fixture.
E2E_ADMIN_PASSWORD = 'e2e-admin-password-not-a-secret'
E2E_ADMIN_HASH = admin_auth.hash_password(E2E_ADMIN_PASSWORD)

#: Synthetic Prusa server host (``.invalid`` is reserved and never resolves).
E2E_PRUSA_SERVER = 'connect.e2e.invalid'

#: Number of synthetic videos/frames; large enough to paginate.
E2E_VIDEO_COUNT = 14
E2E_FRAME_COUNT = 14


#: A real 1x1 baseline JPEG so a browser can actually decode frame previews.
_JPEG_1X1 = base64.b64decode(
    '/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof'
    'Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB'
    'AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=='
)


def _jpeg(width=8, height=8):
    """Return the shared real 1x1 JPEG (ignores the requested dimensions)."""
    return _JPEG_1X1


def _avi(payload_size=200):
    """Return a small but RIFF/AVI-shaped byte string (never played)."""
    payload = bytes(range(256)) * (payload_size // 256 + 1)
    payload = payload[:payload_size]
    return b'RIFF' + struct.pack('<I', 4 + len(payload)) + b'AVI ' + payload


class FakeRuntime:
    """Mutable authoritative runtime state shared by the injected fakes."""

    def __init__(self):
        self.settings = {
            'camera_name': 'E2E Camera',
            'quality_tier': 2,            # HD
            'rotation': 0,
            'snapshot_upload_enabled': True,
            'snapshot_interval': 60,
            'timelapse_enabled': True,
            'timelapse_interval': 30,
            'timelapse_fps': 10,
            'rtsp_mode': 2,               # enabled
            'webrtc_mode': 1,             # enabled
        }
        self.turn_locked = True
        self.counters = {
            'settings': 0, 'mqtt_test': 0, 'mqtt_test_stored_password': 0,
            'reboot': 0, 'ssh': 0, 'recovery': 0, 'reset': 0,
            'update_check': 0, 'update_install': 0, 'build_busy': 0,
        }
        self.scenarios = {
            'live': 'live',            # live | stale
            'build': 'progress',       # progress | busy | empty | error | done
            'mqtt_test': 'ok',         # ok | fail
            'update_check': 'ok',      # ok | busy
            'update_install': 'ok',    # ok | busy
        }

    # -- settings ---------------------------------------------------------- #
    def apply(self, field, value):
        self.counters['settings'] += 1
        settings = self.settings
        if field == 'camera_name':
            if not isinstance(value, str) or not value.strip():
                return self._reject('camera name must not be empty')
            settings['camera_name'] = value.strip()[:64]
        elif field == 'quality':
            names = {'sd': 1, 'hd': 2, 'fhd': 3}
            tier = names.get(value)
            if tier is None:
                return self._reject('unknown quality')
            if self.turn_locked and tier > settings['quality_tier']:
                return self._reject(
                    'quality change refused: a WebRTC viewer holds the TURN '
                    'quality lock; raise it after the stream ends')
            settings['quality_tier'] = tier
        elif field == 'rotation':
            if type(value) is not int or value not in (0, 90, 180, 270):
                return self._reject('rotation must be one of 0, 90, 180, 270')
            settings['rotation'] = value
        elif field == 'snapshot_upload_enabled':
            if not isinstance(value, bool):
                return self._reject('snapshot upload must be true or false')
            settings['snapshot_upload_enabled'] = value
        elif field == 'snapshot_interval':
            if not isinstance(value, int) or not 10 <= value <= 600:
                return self._reject('snapshot interval must be between 10 and 600 seconds')
            settings['snapshot_interval'] = value
        elif field == 'timelapse_enabled':
            if not isinstance(value, bool):
                return self._reject('timelapse capture must be true or false')
            settings['timelapse_enabled'] = value
        elif field == 'timelapse_interval':
            if not isinstance(value, int) or not 1 <= value <= 3600:
                return self._reject('timelapse interval must be between 1 and 3600 seconds')
            settings['timelapse_interval'] = value
        elif field == 'timelapse_fps':
            if not isinstance(value, int) or not 1 <= value <= 30:
                return self._reject('timelapse FPS must be between 1 and 30')
            settings['timelapse_fps'] = value
        elif field == 'rtsp_mode':
            names = {'disabled': 1, 'enabled': 2}
            if value not in names:
                return self._reject('unknown RTSP mode')
            settings['rtsp_mode'] = names[value]
        elif field == 'webrtc_mode':
            names = {'disabled': 0, 'enabled': 1}
            if value not in names:
                return self._reject('unknown WebRTC mode')
            settings['webrtc_mode'] = names[value]
        else:
            return self._reject('setting field is not supported')
        return {
            'ok': True, 'error': '', 'changed': [field],
            'settings': dict(settings), 'degraded': False,
        }

    def _reject(self, reason):
        return {
            'ok': False, 'error': reason, 'changed': [],
            'settings': dict(self.settings), 'degraded': False,
        }

    def dashboard(self):
        s = self.settings
        names = {1: 'SD', 2: 'HD', 3: 'FHD'}
        return {
            'ok': True,
            'runtime': {'source': 'live', 'fresh': True, 'age_seconds': 1.0},
            'camera': {
                'state': 'running',
                'resolution': {'label': '1920×1080'},
                'quality': {'name': names.get(s['quality_tier'], 'unknown')},
            },
            'prusa': {'state': 'authenticated'},
            'snapshots': {'state': 'ok'},
            'rtsp': {'state': 'running'},
            'webrtc': {'state': 'stopped'},
            'mqtt': {'state': 'connected', 'configured': True},
            'storage': {'state': 'ok', 'free_bytes': 3_500_000_000},
            'updates': {'state': 'up-to-date'},
            'metrics': {
                'wifi_rssi_dbm': -52,
                'cpu_temperature_c': 48.3,
                'uptime_seconds': 7200,
            },
            'version': {'release': '1.2.3-e2e', 'application': '1.2.3'},
            'settings': dict(s),
        }


class FakeLiveMonitor:
    """Deterministic stand-in for ``live_monitor.LiveMonitor``."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.touched = []

    def touch(self, viewer_id):
        self.touched.append(viewer_id)
        return True

    def latest(self):
        if self.runtime.scenarios['live'] == 'stale':
            return {'state': 'stale', 'jpeg': None, 'age_seconds': 99.0}
        return {'state': 'live', 'jpeg': _jpeg(), 'age_seconds': 0.2}

    def metrics(self):
        return {
            'source': 'stream_mux:8888',
            'interval_seconds': 1.0,
            'producer': {'running': True, 'frames_produced': 33},
            'viewers': {'active': len(self.touched), 'cap': 4},
            'frame': {'state': self.runtime.scenarios['live']},
        }

    def stop(self):
        pass


class FakeBuildManager:
    """Deterministic stand-in for ``media_build.BuildManager``.

    ``progress`` mode reports a running job once and then done, so the UI's
    busy/progress/complete path is exercised without a real encoder.
    """

    def __init__(self, runtime):
        self.runtime = runtime
        self.job_id = 'e2e' + '0' * 29
        self.polls = 0

    def start(self, fps=10, width=None, height=None):
        mode = self.runtime.scenarios['build']
        if mode == 'busy':
            self.runtime.counters['build_busy'] += 1
            return {'ok': False, 'code': 'busy', 'error': 'a build is already running',
                    'job_id': self.job_id}
        if mode == 'empty':
            return {'ok': False, 'code': 'preflight', 'error': 'no frames to assemble',
                    'job_id': ''}
        self.polls = 0
        state = 'error' if mode == 'error' else 'pending'
        return {'ok': True, 'job': self._job(state, 0)}

    def status(self, job_id):
        if job_id != self.job_id:
            return None
        mode = self.runtime.scenarios['build']
        self.polls += 1
        if mode == 'busy':
            # A pre-existing build keeps running; the UI must follow it rather
            # than claim a new build started.
            return self._job('running', self.polls)
        if mode == 'error':
            return self._job('error', 4, reason='frame decode failed')
        if mode == 'done':
            return self._job('done', 12)
        if self.polls <= 1:
            return self._job('running', 4)
        return self._job('done', 12)

    def _job(self, state, written, reason=''):
        return {
            'id': self.job_id, 'state': state, 'frames_total': 12,
            'frames_written': written, 'reason': reason,
            'started': '2026-01-01T00:00:00Z',
            'finished': None if state in ('pending', 'running') else '2026-01-01T00:00:05Z',
        }

    def stop(self):
        pass


class FakeUpdateManager:
    """Deterministic stand-in for ``update_control.UpdateManager``."""

    def __init__(self, runtime):
        self.runtime = runtime

    def state(self):
        return {
            'state': 'update-available',
            'installed_version': '1.2.3',
            'latest_version': '1.2.4',
            'release_summary': 'Synthetic E2E release notes.',
            'in_progress': False,
            'checking': False,
            'installing': False,
            'last_check': 1_767_225_600.0,
            'source_configured': True,
            'observed_at': time.time(),
            'fresh': True,
        }

    def check(self):
        if self.runtime.scenarios['update_check'] == 'busy':
            return {'ok': False, 'busy': True, 'reason': 'a check is already running'}
        self.runtime.counters['update_check'] += 1
        return {'ok': True, 'started': True}

    def install(self):
        if self.runtime.scenarios['update_install'] == 'busy':
            return {'ok': False, 'busy': True, 'reason': 'an install is already running'}
        self.runtime.counters['update_install'] += 1
        return {'ok': True, 'started': True}

    def close(self):
        pass


class FakeDiagnosticsProvider:
    """Deterministic stand-in for ``diagnostics.DiagnosticsProvider``."""

    def __init__(self):
        self.secrets = ()

    def __call__(self, secrets=()):
        self.secrets = tuple(secrets or ())
        text = (
            '-- pibuddycam.service --\n'
            'E2E synthetic diagnostics; no device or secret is involved.\n'
        )
        return {
            'ok': True, 'available': True, 'state': 'ready',
            'text': text, 'lines': 2, 'bytes': len(text.encode()),
            'truncated': False, 'fresh': True,
        }

    def stop(self):
        pass


class FakeReboot:
    """Records fixed-verb reboot calls without rebooting anything."""

    def __init__(self, runtime):
        self.runtime = runtime

    def __call__(self):
        self.runtime.counters['reboot'] += 1
        return SimpleNamespace(ok=True, reason='')


class FakeSshRunner:
    """Records ``systemctl`` argv; never runs a process."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.calls = []

    def __call__(self, args, timeout):
        argv = list(args)
        self.calls.append(argv)
        # Count only real mutations (enable/disable), not the read-only
        # ``is-enabled`` probe the System view runs on load.
        if len(argv) >= 2 and argv[1] in ('enable', 'disable') and '--now' in argv:
            self.runtime.counters['ssh'] += 1
        return SimpleNamespace(returncode=0, stdout='', stderr='')


class FakeResetReport:
    def __init__(self):
        self.ok = True
        self.reason = ''
        self.action = 'reset'
        self.file_count = 7
        self.backup_path = '/tmp/e2e-backup'

    def to_dict(self):
        return {
            'ok': True, 'action': 'reset', 'file_count': self.file_count,
            'backup_path': self.backup_path,
            'included_timelapse': True,
            'safety_backup': True,
        }


class FakeFactoryReset:
    """Records factory-reset steps and returns a synthetic report."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.begun = 0
        self.confirmed = 0

    def begin(self, reason=''):
        self.runtime.counters['reset'] += 1
        self.begun += 1
        return 'e2e-reset-token'

    def confirm(self, token):
        self.confirmed += 1
        return True

    def execute(self, token='', include_timelapse=True):
        return FakeResetReport()


class Environment:
    """One synthetic temp tree plus the app/fakes bound to it."""

    def __init__(self, media='default', scenarios=None):
        self.root = tempfile.TemporaryDirectory(prefix='pibuddycam-e2e-')
        base = Path(self.root.name)
        self.device_path = base / 'device.toml'
        self.secrets_path = base / 'secrets.toml'
        self.provisioning_path = base / 'provisioning.json'
        self.recovery_path = base / 'pibuddycam-recovery'
        self.media_dir = base / 'timelapse'
        self.media_dir.mkdir()
        self.runtime = FakeRuntime()
        if scenarios:
            self.runtime.scenarios.update(scenarios)
        self._seed_config()
        self._seed_media(media)
        self.app = self._build_app()

    def _seed_config(self):
        device = config_schema.default_device()
        device['camera_name'] = 'E2E Camera'
        device['fingerprint'] = 'aabbccddee01'
        device['prusa']['server'] = E2E_PRUSA_SERVER
        device['admin']['hostname'] = 'pibuddycam-e2e.local'
        self.device_path.write_text(
            config_schema.dumps_device(device), encoding='utf-8')
        secrets = {'admin': {'password_hash': E2E_ADMIN_HASH}}
        self.secrets_path.write_text(
            config_schema.dumps_secrets(secrets), encoding='utf-8')
        self.provisioning_path.write_text(
            json.dumps({'state': 'claimed'}), encoding='utf-8')

    def _seed_media(self, media):
        if media == 'empty':
            return
        statuses = ['D', 'E', 'P', 'U']
        lines = []
        for index in range(E2E_VIDEO_COUNT):
            name = f'timelapse_00-{index // 60:02d}-{index % 60:02d}-000.avi'
            (self.media_dir / name).write_bytes(_avi(120 + index))
            lines.append(f'{name}:{statuses[index % len(statuses)]}')
        # The hidden index is authoritative for status but is never served.
        (self.media_dir / '.timelapse_videos.csv').write_text(
            '\n'.join(lines) + '\n', encoding='utf-8')
        for index in range(E2E_FRAME_COUNT):
            name = f'timelapse_00-{index // 60:02d}-{index % 60:02d}-000.jpg'
            (self.media_dir / name).write_bytes(_jpeg(8 + index, 8 + index))

    def _build_app(self):
        self.live_monitor = FakeLiveMonitor(self.runtime)
        self.build_manager = FakeBuildManager(self.runtime)
        self.update_manager = FakeUpdateManager(self.runtime)
        self.diagnostics = FakeDiagnosticsProvider()
        self.reboot = FakeReboot(self.runtime)
        self.ssh_runner = FakeSshRunner(self.runtime)
        self.factory_reset = FakeFactoryReset(self.runtime)
        return admin_http.AdminApp(
            mode='admin',
            admin_hash=E2E_ADMIN_HASH,
            provisioning_state=provisioning.ProvisioningState(state='claimed'),
            device_path=str(self.device_path),
            secrets_path=str(self.secrets_path),
            provisioning_path=str(self.provisioning_path),
            recovery_path=str(self.recovery_path),
            device_id='e2e0001',
            dashboard_provider=self.runtime.dashboard,
            settings_actions=self.runtime.apply,
            live_monitor=self.live_monitor,
            media_dir=str(self.media_dir),
            build_manager=self.build_manager,
            update_manager=self.update_manager,
            diagnostics_provider=self.diagnostics,
            reboot_fn=self.reboot,
            ssh_runner=self.ssh_runner,
            factory_reset=self.factory_reset,
            mqtt_probe=self._mqtt_probe,
            release_identity_fn=lambda: {
                'version': '1.2.3', 'source_commit': 'e2e0000',
                'os_suite': 'trixie', 'kernel_package': 'linux-image-e2e',
            },
            application_version_fn=lambda: '1.2.3',
            hostname_fn=lambda: 'pibuddycam-e2e',
        )

    def _mqtt_probe(self, config):
        self.runtime.counters['mqtt_test'] += 1
        if getattr(config, 'password', ''):
            self.runtime.counters['mqtt_test_stored_password'] += 1
        if self.runtime.scenarios['mqtt_test'] == 'fail':
            return (False, 'synthetic broker refused the connection')
        return (True, 'synthetic broker accepted the connection')

    def close(self):
        try:
            self.app.close()
        except Exception:  # noqa: BLE001 - teardown must never raise
            pass
        self.root.cleanup()


class HarnessState:
    """The live harness state: current environment plus a lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.env = Environment()

    def rebuild(self, media='default', scenarios=None):
        with self.lock:
            self.env.close()
            self.env = Environment(media=media, scenarios=scenarios)
            return self.env

    def current(self):
        return self.env


class AdminRequestHandler(BaseHTTPRequestHandler):
    """Translate one HTTP request into a real ``admin_http.Request``."""

    server_version = 'Buddy3E2E/1.0'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):  # noqa: A003 - silence the access log
        return

    # -- HTTP verbs -------------------------------------------------------- #
    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_PUT(self):
        self._handle('PUT')

    def do_PATCH(self):
        self._handle('PATCH')

    def do_DELETE(self):
        self._handle('DELETE')

    # -- dispatch ---------------------------------------------------------- #
    def _handle(self, method):
        try:
            body = self._read_body()
        except ValueError:
            self._write_json(413, {'ok': False, 'error': 'request body too large'})
            return
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path or '/'
        if path.startswith('/__e2e/'):
            self._handle_control(method, path, body)
            return
        query = {
            key: values[0]
            for key, values in urllib.parse.parse_qs(
                parts.query, keep_blank_values=True).items()
        }
        request = admin_http.Request(
            method=method,
            path=path,
            query=query,
            headers=dict(self.headers),
            body=body,
            peer_ip=self.client_address[0] if self.client_address else '',
        )
        state = self.server.state
        with state.lock:
            app = state.env.app
            response = app.handle(request)
        self._write_core(response)

    def _read_body(self):
        raw_length = self.headers.get('Content-Length')
        if not raw_length:
            return b''
        try:
            length = int(raw_length)
        except ValueError:
            return b''
        if length < 0 or length > 1_000_000:
            raise ValueError('body too large')
        return self.rfile.read(length) if length else b''

    # -- control surface --------------------------------------------------- #
    def _handle_control(self, method, path, body):
        state = self.server.state
        try:
            payload = json.loads(body.decode('utf-8')) if body else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        action = path[len('/__e2e/'):]

        if method == 'POST' and action == 'reset':
            env = state.rebuild(
                media=payload.get('media', 'default'),
                scenarios=payload.get('scenarios'),
            )
            self._write_json(200, {'ok': True, 'hostname': 'pibuddycam-e2e'})
            return
        if method == 'POST' and action == 'expire':
            with state.lock:
                state.current().app._sessions.revoke_all()
            self._write_json(200, {'ok': True})
            return
        if method == 'POST' and action == 'scenario':
            with state.lock:
                env = state.current()
                env.runtime.scenarios.update(payload)
                scenarios = dict(env.runtime.scenarios)
            self._write_json(200, {'ok': True, 'scenarios': scenarios})
            return
        if method == 'GET' and action == 'counters':
            with state.lock:
                env = state.current()
                counters = dict(env.runtime.counters)
                scenarios = dict(env.runtime.scenarios)
                recovery_sentinel = env.recovery_path.exists()
            self._write_json(200, {
                'ok': True, 'counters': counters, 'scenarios': scenarios,
                'recovery_sentinel': recovery_sentinel,
            })
            return
        self._write_json(404, {'ok': False, 'error': 'unknown e2e control'})

    # -- writers ----------------------------------------------------------- #
    def _write_json(self, status, payload):
        data = json.dumps(payload).encode('utf-8')
        self._write_bytes(status, {'Content-Type': 'application/json'}, data)

    def _write_core(self, response):
        body = response.body or b''
        handle = getattr(response, 'file', None)
        length = len(body)
        if handle is not None:
            length = int(getattr(response, 'file_length', 0) or 0)
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        if 'Content-Length' not in response.headers:
            self.send_header('Content-Length', str(length))
        self.end_headers()
        if handle is not None:
            try:
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            finally:
                handle.close()
            return
        if body:
            self.wfile.write(body)

    def _write_bytes(self, status, headers, data):
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)


class HarnessServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, state):
        super().__init__(address, handler)
        self.state = state


def build_server(host='127.0.0.1', port=0):
    """Return ``(server, state)`` with a fresh environment, not yet serving."""
    state = HarnessState()
    server = HarnessServer((host, port), AdminRequestHandler, state)
    return server, state


def base_url(server):
    host, port = server.server_address[:2]
    return f'http://{host}:{port}'


def serve_forever(server):
    server.serve_forever(poll_interval=0.2)


def main(argv=None):
    """CLI entry point: serve until SIGINT/SIGTERM and clean up."""
    import argparse

    parser = argparse.ArgumentParser(description='PiBuddyCam admin UI E2E server')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args(argv)

    server, state = build_server(args.host, args.port)
    url = base_url(server)
    print(f'E2E_READY {url}', flush=True)
    thread = threading.Thread(target=serve_forever, args=(server,), daemon=True)
    thread.start()

    stop = threading.Event()

    def _shutdown(*_args):
        stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        while not stop.wait(0.3):
            pass
    finally:
        server.shutdown()
        server.server_close()
        state.current().close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
