"""WP-UI2 (AC-4/AC-14/AC-17/AC-18): authenticated ``GET /api/dashboard``.

Stdlib-only: drives the transport-neutral :mod:`admin_http` core with an
injected provider. No ``aiohttp`` import, no socket, no live runtime.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import admin_auth  # noqa: E402
import admin_http  # noqa: E402
import provisioning  # noqa: E402

ADMIN_PASSWORD = 'correct horse battery staple'
ADMIN_HASH = admin_auth.hash_password(ADMIN_PASSWORD)
SESSION_COOKIE = admin_auth.SESSION_COOKIE_NAME
NOW = 1000.0


def _make_request(method, path, body=None, headers=None, peer_ip='10.0.0.5',
                  now=NOW):
    raw = b''
    merged = {}
    if isinstance(body, dict):
        raw = json.dumps(body).encode('utf-8')
        merged['Content-Type'] = 'application/json'
    merged.update(headers or {})
    return admin_http.Request(method, path, {}, merged, raw, peer_ip, now)


def _parse_cookie(response):
    raw = response.headers.get('Set-Cookie', '')
    for part in raw.split(';'):
        name, _, value = part.strip().partition('=')
        if name == SESSION_COOKIE:
            return value
    return ''


class DashboardRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _build_app(self, provider=None):
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
            dashboard_provider=provider,
        )

    def _login(self, app):
        response = app.handle(_make_request(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD}))
        self.assertEqual(response.status, 200, response.body)
        return _parse_cookie(response)

    def test_route_requires_authentication(self):
        app = self._build_app(provider=lambda: {'ok': True})
        response = app.handle(_make_request('GET', '/api/dashboard'))
        self.assertEqual(response.status, 401)

    def test_authenticated_route_returns_provider_payload(self):
        app = self._build_app(provider=lambda: {
            'ok': True,
            'runtime': {'source': 'live', 'fresh': True},
            'camera': {'state': 'running'},
        })
        token = self._login(app)
        response = app.handle(_make_request(
            'GET', '/api/dashboard',
            headers={'Cookie': f'{SESSION_COOKIE}={token}'}))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['camera']['state'], 'running')
        self.assertIn('trusted_lan', payload)
        self.assertIn('notice', payload['trusted_lan'])

    def test_provider_exception_degrades_gracefully(self):
        def boom():
            raise RuntimeError('provider exploded')

        app = self._build_app(provider=boom)
        token = self._login(app)
        response = app.handle(_make_request(
            'GET', '/api/dashboard',
            headers={'Cookie': f'{SESSION_COOKIE}={token}'}))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload['runtime']['source'], 'unavailable')
        self.assertFalse(payload['runtime']['fresh'])
        self.assertIn('trusted_lan', payload)

    def test_non_dict_provider_result_degrades(self):
        app = self._build_app(provider=lambda: ['not', 'a', 'dict'])
        token = self._login(app)
        response = app.handle(_make_request(
            'GET', '/api/dashboard',
            headers={'Cookie': f'{SESSION_COOKIE}={token}'}))
        payload = json.loads(response.body)
        self.assertEqual(payload['runtime']['source'], 'unavailable')

    def test_missing_provider_degrades(self):
        app = self._build_app(provider=None)
        token = self._login(app)
        response = app.handle(_make_request(
            'GET', '/api/dashboard',
            headers={'Cookie': f'{SESSION_COOKIE}={token}'}))
        payload = json.loads(response.body)
        self.assertEqual(payload['runtime']['source'], 'unavailable')

    def test_provider_payload_is_not_mutated(self):
        original = {'ok': True, 'runtime': {'source': 'live'}}
        app = self._build_app(provider=lambda: original)
        token = self._login(app)
        app.handle(_make_request(
            'GET', '/api/dashboard',
            headers={'Cookie': f'{SESSION_COOKIE}={token}'}))
        self.assertNotIn('trusted_lan', original)

    def test_secret_keys_are_redacted(self):
        canary = 'CANARY-TOKEN-abc'
        app = self._build_app(provider=lambda: {
            'ok': True,
            'token': canary,
            'nested': {'password': 'CANARY-PASSWORD-def'},
        })
        token = self._login(app)
        body = app.handle(_make_request(
            'GET', '/api/dashboard',
            headers={'Cookie': f'{SESSION_COOKIE}={token}'})).body.decode('utf-8')
        self.assertNotIn(canary, body)
        self.assertNotIn('CANARY-PASSWORD-def', body)

    def test_oversized_provider_value_is_bounded(self):
        app = self._build_app(provider=lambda: {
            'ok': True,
            'settings': {'camera_name': 'x' * 100000},
        })
        token = self._login(app)
        response = app.handle(_make_request(
            'GET', '/api/dashboard',
            headers={'Cookie': f'{SESSION_COOKIE}={token}'}))
        payload = json.loads(response.body)
        self.assertLessEqual(
            len(payload['settings']['camera_name']),
            admin_http.runtime_ipc.MAX_TEXT_CHARS,
        )

    def test_cookie_and_auth_header_canaries_are_not_echoed(self):
        app = self._build_app(provider=lambda: {'ok': True})
        token = self._login(app)
        response = app.handle(_make_request(
            'GET', '/api/dashboard',
            headers={
                'Cookie': f'{SESSION_COOKIE}={token}',
                'Authorization': 'Bearer CANARY-AUTH-HEADER-xyz',
                'X-Camera-Token': 'CANARY-CAMERA-TOKEN-xyz',
            }))
        body = response.body.decode('utf-8')
        for canary in (
            'CANARY-AUTH-HEADER-xyz', 'CANARY-CAMERA-TOKEN-xyz', token,
        ):
            self.assertNotIn(canary, body, canary)

    def test_dashboard_route_is_in_the_core_route_table(self):
        routes = {
            (route.method, route.pattern.pattern)
            for route in admin_http.AdminApp()._routes
        }
        self.assertIn(('GET', r'^/api/dashboard$'), routes)

    def test_dashboard_route_is_authenticated_policy(self):
        route = next(
            route for route in admin_http.AdminApp()._routes
            if route.pattern.pattern == r'^/api/dashboard$')
        self.assertEqual(route.policy, 'authenticated')


if __name__ == '__main__':
    unittest.main()
