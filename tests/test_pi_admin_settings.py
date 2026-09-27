"""WP-UI3 (AC-5/AC-6/AC-7/AC-17/AC-18): camera settings mutations.

Three layers are pinned together:

* ``SettingsCoordinator.apply_mutation`` -- the allowlisted dispatch that maps
  UI wire values onto the existing setters (one coordinator, no direct state
  write);
* ``PATCH /api/settings`` in the stdlib admin core -- auth + CSRF, strict
  bounds/unknown/type rejection, authoritative state on success and rejection,
  and honest 503 when the runtime is unavailable; and
* the runtime IPC ``settings.set`` operation allowlist.

Stdlib-only; the coordinator uses recording fakes and no filesystem effects.
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(PI_DIR))

import admin_auth  # noqa: E402
import admin_http  # noqa: E402
import provisioning  # noqa: E402
import quality_control  # noqa: E402
import rtsp_control  # noqa: E402
import runtime_ipc  # noqa: E402
import settings_coordinator  # noqa: E402
from settings_coordinator import SettingsCoordinator  # noqa: E402
from state import CameraState, RAW_TO_ENUM  # noqa: E402

ADMIN_PASSWORD = 'correct horse battery staple'
ADMIN_HASH = admin_auth.hash_password(ADMIN_PASSWORD)
SESSION_COOKIE = admin_auth.SESSION_COOKIE_NAME
NOW = 1000.0


class Recorder:
    def __init__(self, return_value=None):
        self.return_value = return_value
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.return_value


class LiveQuality:
    def __init__(self, state):
        self.state = state
        self.calls = []

    def __call__(self, raw):
        self.calls.append(raw)
        if raw in RAW_TO_ENUM:
            self.state.quality = RAW_TO_ENUM[raw]
        return True


def _coordinator(state=None):
    state = state or CameraState()
    live = LiveQuality(state)
    coordinator = SettingsCoordinator(
        state,
        persist=Recorder(True),
        publish=Recorder(None),
        quality_apply=live,
        quality_persist=Recorder(None),
        rtsp_start=Recorder(None),
        rtsp_stop=Recorder(None),
        webrtc_start=Recorder(None),
        webrtc_stop=Recorder(None),
    )
    return state, coordinator, live


class CoordinatorMutationTests(unittest.TestCase):
    def setUp(self):
        self._write_mode = patch.object(rtsp_control, 'write_mode', return_value=True)
        self._write_mode.start()
        self.addCleanup(self._write_mode.stop)

    def test_quality_names_map_to_raw_bytes(self):
        for name, raw in (('sd', 5), ('hd', 6), ('fhd', 7)):
            with self.subTest(name=name):
                state, coordinator, live = _coordinator()
                result = coordinator.apply_mutation('quality', name)
                self.assertTrue(result.ok)
                self.assertEqual(live.calls, [raw])
                self.assertEqual(state.quality, RAW_TO_ENUM[raw])

    def test_camera_name_round_trip(self):
        state, coordinator, _ = _coordinator()
        result = coordinator.apply_mutation('camera_name', '  Bench Cam  ')
        self.assertTrue(result.ok)
        self.assertEqual(state.camera_name, 'Bench Cam')
        self.assertEqual(result.changed, ['camera_name'])

    def test_boolean_and_numeric_fields(self):
        state, coordinator, _ = _coordinator()
        self.assertTrue(coordinator.apply_mutation('snapshot_upload_enabled', True).ok)
        self.assertTrue(state.snapshot_upload_enabled)
        self.assertTrue(coordinator.apply_mutation('snapshot_interval', 45).ok)
        self.assertEqual(state.snapshot_interval, 45)
        self.assertTrue(coordinator.apply_mutation('timelapse_enabled', True).ok)
        self.assertTrue(state.timelapse_enabled)
        self.assertTrue(coordinator.apply_mutation('timelapse_interval', 120).ok)
        self.assertEqual(state.timelapse_interval, 120)
        self.assertTrue(coordinator.apply_mutation('timelapse_fps', 24).ok)
        self.assertEqual(state.timelapse_fps, 24)

    def test_mode_fields(self):
        state, coordinator, _ = _coordinator()
        self.assertTrue(coordinator.apply_mutation('rtsp_mode', 'enabled').ok)
        self.assertEqual(state.rtsp_mode, 2)
        self.assertTrue(coordinator.apply_mutation('rtsp_mode', 'disabled').ok)
        self.assertEqual(state.rtsp_mode, 1)
        self.assertTrue(coordinator.apply_mutation('webrtc_mode', 'enabled').ok)
        self.assertEqual(state.webrtc_mode, 1)
        self.assertTrue(coordinator.apply_mutation('webrtc_mode', 'disabled').ok)
        self.assertEqual(state.webrtc_mode, 0)

    def test_unknown_field_rejected_with_authoritative_state(self):
        state, coordinator, _ = _coordinator()
        state.camera_name = 'Original'
        result = coordinator.apply_mutation('is_admin', True)
        self.assertFalse(result.ok)
        self.assertEqual(state.camera_name, 'Original')
        self.assertEqual(result.state['camera_name'], 'Original')

    def test_type_confusion_rejected_without_mutation(self):
        state, coordinator, _ = _coordinator()
        for field, value in (
            ('snapshot_upload_enabled', 'yes'),
            ('snapshot_interval', '45'),
            ('timelapse_enabled', 1),
            ('timelapse_fps', True),
            ('quality', 7),
            ('rtsp_mode', 2),
            ('webrtc_mode', 'on'),
        ):
            with self.subTest(field=field):
                result = coordinator.apply_mutation(field, value)
                self.assertFalse(result.ok, field)
        self.assertEqual(state.snapshot_interval, 10)
        self.assertEqual(state.timelapse_fps, 10)

    def test_out_of_range_rejected(self):
        state, coordinator, _ = _coordinator()
        for field, value in (
            ('snapshot_interval', 9),
            ('snapshot_interval', 601),
            ('timelapse_interval', 0),
            ('timelapse_interval', 3601),
            ('timelapse_fps', 0),
            ('timelapse_fps', 31),
            ('camera_name', '   '),
        ):
            with self.subTest(field=field, value=value):
                self.assertFalse(coordinator.apply_mutation(field, value).ok)

    def test_turn_quality_lock_rejects_raise_and_keeps_state(self):
        state, coordinator, _ = _coordinator()
        state.set_quality(2)  # HD
        state.turn_online = True
        result = coordinator.apply_mutation('quality', 'fhd')
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, quality_control.TURN_QUALITY_LOCK_LOG)
        self.assertEqual(state.quality, 2)
        self.assertEqual(result.state['quality_tier'], 2)

    def test_turn_quality_lock_allows_lower(self):
        state, coordinator, _ = _coordinator()
        state.set_quality(3)  # FHD
        state.turn_online = True
        result = coordinator.apply_mutation('quality', 'hd')
        self.assertTrue(result.ok)
        self.assertEqual(state.quality, 2)

    def test_never_raises_on_bad_input(self):
        _, coordinator, _ = _coordinator()
        for field, value in (
            (None, 1), ([], 1), ({}, 1), ('camera_name', ['x']),
            ('quality', None), ('snapshot_interval', 1.5),
        ):
            result = coordinator.apply_mutation(field, value)
            self.assertFalse(result.ok)
            self.assertIsInstance(result.state, dict)

    def test_mutation_fields_allowlist_is_exact(self):
        self.assertEqual(
            set(settings_coordinator.MUTATION_FIELDS),
            {
                'camera_name', 'quality', 'rotation', 'snapshot_upload_enabled',
                'snapshot_interval', 'timelapse_enabled', 'timelapse_interval',
                'timelapse_fps', 'rtsp_mode', 'webrtc_mode',
            },
        )


# --------------------------------------------------------------------------- #
# PATCH /api/settings core API                                                #
# --------------------------------------------------------------------------- #

def _make_request(method, path, body=None, headers=None, peer_ip='10.0.0.5', now=NOW):
    raw = b''
    merged = {}
    if isinstance(body, dict):
        raw = json.dumps(body).encode('utf-8')
        merged['Content-Type'] = 'application/json'
    elif isinstance(body, bytes):
        raw = body
    merged.update(headers or {})
    return admin_http.Request(method, path, {}, merged, raw, peer_ip, now)


def _parse_cookie(response):
    raw = response.headers.get('Set-Cookie', '')
    for part in raw.split(';'):
        name, _, value = part.strip().partition('=')
        if name == SESSION_COOKIE:
            return value
    return ''


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _build_app(self, settings_actions=None):
        return admin_http.AdminApp(
            mode='admin',
            sessions=admin_auth.SessionStore(),
            limiter=admin_auth.LoginRateLimiter(),
            admin_hash=ADMIN_HASH,
            provisioning_state=provisioning.ProvisioningState(state='claimed'),
            device_path=str(self.root / 'device.toml'),
            secrets_path=str(self.root / 'secrets.toml'),
            provisioning_path=str(self.root / 'provisioning.json'),
            clock=lambda: NOW,
            settings_actions=settings_actions,
        )

    def _login(self, app):
        response = app.handle(_make_request(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD}))
        self.assertEqual(response.status, 200, response.body)
        token = _parse_cookie(response)
        csrf = json.loads(response.body)['csrf']
        return token, csrf

    def _authed(self, app, method, path, body=None, csrf=True):
        token, token_csrf = self._login(app)
        headers = {'Cookie': f'{SESSION_COOKIE}={token}'}
        if csrf:
            headers['X-CSRF-Token'] = token_csrf
        return app.handle(_make_request(method, path, body=body, headers=headers))

    def test_requires_authentication(self):
        app = self._build_app(settings_actions=lambda f, v: {'ok': True})
        response = app.handle(_make_request('PATCH', '/api/settings', body={'field': 'camera_name', 'value': 'x'}))
        self.assertEqual(response.status, 401)

    def test_requires_csrf(self):
        app = self._build_app(settings_actions=lambda f, v: {'ok': True})
        response = self._authed(
            app, 'PATCH', '/api/settings',
            body={'field': 'camera_name', 'value': 'x'}, csrf=False)
        self.assertEqual(response.status, 403)

    def test_success_returns_authoritative_state(self):
        calls = []

        def action(field, value):
            calls.append((field, value))
            return {
                'ok': True, 'changed': [field],
                'settings': {'camera_name': value}, 'degraded': False,
            }

        app = self._build_app(settings_actions=action)
        response = self._authed(
            app, 'PATCH', '/api/settings',
            body={'field': 'camera_name', 'value': 'Bench'})
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['field'], 'camera_name')
        self.assertEqual(payload['settings']['camera_name'], 'Bench')
        self.assertEqual(calls, [('camera_name', 'Bench')])

    def test_rejection_returns_state_and_no_false_success(self):
        def action(field, value):
            return {
                'ok': False,
                'error': quality_control.TURN_QUALITY_LOCK_LOG,
                'changed': [],
                'settings': {'quality_tier': 2},
                'degraded': False,
            }

        app = self._build_app(settings_actions=action)
        response = self._authed(
            app, 'PATCH', '/api/settings',
            body={'field': 'quality', 'value': 'fhd'})
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['error'], quality_control.TURN_QUALITY_LOCK_LOG)
        self.assertEqual(payload['settings']['quality_tier'], 2)

    def test_unknown_field_is_rejected_before_dispatch(self):
        called = []
        app = self._build_app(settings_actions=lambda f, v: called.append(f) or {'ok': True})
        response = self._authed(
            app, 'PATCH', '/api/settings',
            body={'field': 'is_admin', 'value': True})
        self.assertEqual(response.status, 400)
        self.assertEqual(called, [])

    def test_unknown_body_key_is_rejected(self):
        app = self._build_app(settings_actions=lambda f, v: {'ok': True})
        response = self._authed(
            app, 'PATCH', '/api/settings',
            body={'field': 'camera_name', 'value': 'x', 'extra': 1})
        self.assertEqual(response.status, 400)

    def test_type_confusion_is_rejected(self):
        app = self._build_app(settings_actions=lambda f, v: {'ok': True})
        response = self._authed(
            app, 'PATCH', '/api/settings',
            body={'field': 'camera_name', 'value': {'nested': True}})
        self.assertEqual(response.status, 400)

    def test_oversize_value_and_body_are_rejected(self):
        app = self._build_app(settings_actions=lambda f, v: {'ok': True})
        # A value over the per-field cap but under the body cap is a 400.
        response = self._authed(
            app, 'PATCH', '/api/settings',
            body={'field': 'camera_name', 'value': 'x' * 600})
        self.assertEqual(response.status, 400)
        # A body over the route cap is rejected before parsing.
        big = b'{"field":"camera_name","value":"' + b'x' * 5000 + b'"}'
        response = self._authed(app, 'PATCH', '/api/settings', body=big)
        self.assertEqual(response.status, 413)

    def test_runtime_unavailable_returns_503(self):
        app = self._build_app(settings_actions=None)
        response = self._authed(
            app, 'PATCH', '/api/settings',
            body={'field': 'camera_name', 'value': 'Bench'})
        self.assertEqual(response.status, 503)
        payload = json.loads(response.body)
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['runtime'], 'unavailable')

    def test_degraded_action_returns_503(self):
        app = self._build_app(settings_actions=lambda f, v: {
            'ok': False, 'error': 'runtime unavailable', 'degraded': True})
        response = self._authed(
            app, 'PATCH', '/api/settings',
            body={'field': 'camera_name', 'value': 'Bench'})
        self.assertEqual(response.status, 503)

    def test_action_exception_returns_503(self):
        def boom(field, value):
            raise RuntimeError('exploded')

        app = self._build_app(settings_actions=boom)
        response = self._authed(
            app, 'PATCH', '/api/settings',
            body={'field': 'camera_name', 'value': 'Bench'})
        self.assertEqual(response.status, 503)

    def test_admin_core_never_writes_state_json(self):
        source = (PI_DIR / 'admin_http.py').read_text(encoding='utf-8')
        # No durable-state writer is imported or called from the admin core; the
        # only settings mutation path is the injected runtime action.
        self.assertNotIn('settings_store', source)
        self.assertNotIn('save_persisted_state', source)
        self.assertNotIn('persistable_state', source)
        self.assertIn('self._settings_actions', source)

    def test_route_is_authenticated_and_declared(self):
        routes = {
            (route.method, route.pattern.pattern) for route in admin_http.AdminApp()._routes
        }
        self.assertIn(('PATCH', r'^/api/settings$'), routes)
        route = next(
            route for route in admin_http.AdminApp()._routes
            if route.pattern.pattern == r'^/api/settings$')
        self.assertEqual(route.policy, 'authenticated')


# --------------------------------------------------------------------------- #
# Runtime IPC settings.set operation                                          #
# --------------------------------------------------------------------------- #

class RuntimeIpcSettingsOperationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.socket_path = str(Path(self._tmp.name) / 'control.sock')

    def _server(self, handlers):
        server = runtime_ipc.RuntimeServer(self.socket_path, handlers=handlers)
        server.start()
        self.addCleanup(server.stop)
        return server

    def test_settings_set_is_an_allowlisted_write_operation(self):
        self.assertIn('settings.set', runtime_ipc.WRITE_OPERATIONS)
        self.assertIn('settings.set', runtime_ipc.ALLOWED_OPERATIONS)
        self.assertNotIn('settings.set', runtime_ipc.READ_OPERATIONS)

    def test_settings_set_round_trip(self):
        captured = []

        def handler(params):
            captured.append(params)
            return {
                'ok': True, 'reason': '', 'changed': [params['field']],
                'settings': {'camera_name': params['value']},
            }

        self._server({'settings.set': handler})
        client = runtime_ipc.RuntimeClient(self.socket_path)
        result = client.settings_set('camera_name', 'Bench')
        self.assertTrue(result['ok'])
        self.assertEqual(captured, [{'field': 'camera_name', 'value': 'Bench'}])
        self.assertEqual(result['data']['settings']['camera_name'], 'Bench')

    def test_settings_set_rejection_is_returned_as_data(self):
        self._server({'settings.set': lambda params: {
            'ok': False, 'reason': 'unknown setting field', 'changed': [], 'settings': {}}})
        client = runtime_ipc.RuntimeClient(self.socket_path)
        result = client.settings_set('bogus', 1)
        self.assertTrue(result['ok'])
        self.assertFalse(result['data']['ok'])
        self.assertEqual(result['data']['reason'], 'unknown setting field')

    def test_unknown_operation_is_rejected(self):
        self._server({})
        client = runtime_ipc.RuntimeClient(self.socket_path)
        result = client.request('settings.delete_everything')
        self.assertFalse(result['ok'])
        self.assertTrue(result['degraded'])

    def test_non_allowlisted_handler_is_not_registered(self):
        server = runtime_ipc.RuntimeServer(
            self.socket_path, handlers={'arbitrary': lambda params: {'ok': True}})
        self.assertNotIn('arbitrary', server._handlers)

    def test_missing_socket_is_degraded(self):
        client = runtime_ipc.RuntimeClient(str(Path(self._tmp.name) / 'absent.sock'))
        result = client.settings_set('camera_name', 'Bench')
        self.assertFalse(result['ok'])
        self.assertTrue(result['degraded'])


if __name__ == '__main__':
    unittest.main()
