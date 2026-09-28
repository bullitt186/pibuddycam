"""WP-UI8 (AC-18/AC-19): the local E2E harness is faithful and hermetic.

These tests run with the standard library only and start no browser. They prove
that the harness in ``tests/e2e/server.py`` serves the real admin core and assets,
exercises real session/CSRF/re-auth/TURN-lock policy, and never touches the host.
"""
import importlib.util
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PI_DIR = REPO_ROOT / 'app'
if str(PI_DIR) not in sys.path:
    sys.path.insert(0, str(PI_DIR))

# Import the harness by path so ``tests/e2e`` needs no package marker.
_spec = importlib.util.spec_from_file_location(
    'e2e_server', REPO_ROOT / 'tests' / 'e2e' / 'server.py')
e2e_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e_server)


class HarnessClient:
    """A tiny cookie-aware client that can send the Secure session cookie."""

    def __init__(self, base):
        self.base = base
        self.cookie = ''
        self.csrf = None

    def call(self, method, path, body=None, csrf=False):
        data = json.dumps(body).encode('utf-8') if body is not None else None
        headers = {}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        if self.cookie:
            headers['Cookie'] = self.cookie
        if csrf and self.csrf:
            headers['X-CSRF-Token'] = self.csrf
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        payload = response.read()
        set_cookie = response.headers.get('Set-Cookie', '')
        if set_cookie:
            self.cookie = set_cookie.split(';', 1)[0]
        return response.status, dict(response.headers), payload

    def json(self, method, path, body=None, csrf=False):
        status, headers, payload = self.call(method, path, body=body, csrf=csrf)
        return status, headers, json.loads(payload) if payload else None


class E2EHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.state = e2e_server.build_server()
        cls.thread = threading.Thread(
            target=e2e_server.serve_forever, args=(cls.server,), daemon=True)
        cls.thread.start()
        cls.base = e2e_server.base_url(cls.server)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.state.current().close()

    def setUp(self):
        # Every test starts from a fresh, deterministic environment.
        client = HarnessClient(self.base)
        status, _headers, _payload = client.call('POST', '/__e2e/reset', body={})
        self.assertEqual(status, 200)

    def login(self):
        client = HarnessClient(self.base)
        status, headers, payload = client.json(
            'POST', '/api/login', body={'password': e2e_server.E2E_ADMIN_PASSWORD})
        self.assertEqual(status, 200, payload)
        client.csrf = payload['csrf']
        self.assertIn('HttpOnly', headers.get('Set-Cookie', ''))
        self.assertIn('Secure', headers.get('Set-Cookie', ''))
        return client

    def test_admin_shell_and_assets_are_served_with_local_csp(self):
        status, headers, payload = HarnessClient(self.base).call('GET', '/admin')
        self.assertEqual(status, 200)
        text = payload.decode('utf-8')
        self.assertIn('PiBuddyCam', text)
        self.assertNotIn('__ASSET_VERSION__', text)
        self.assertIn("default-src 'none'", headers.get('Content-Security-Policy', ''))
        for asset in e2e_server.admin_http.ASSET_ALLOWLIST:
            with self.subTest(asset=asset):
                status, asset_headers, body = HarnessClient(self.base).call(
                    'GET', f'/assets/{asset}')
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertEqual(
                    asset_headers.get('Content-Type'),
                    e2e_server.admin_http.ASSET_ALLOWLIST[asset])

    def test_unauthenticated_api_is_denied(self):
        for path in ('/api/dashboard', '/api/live/frame', '/api/system',
                     '/api/media/timelapses', '/api/diagnostics'):
            with self.subTest(path=path):
                status, _headers, payload = HarnessClient(self.base).call('GET', path)
                self.assertEqual(status, 401)
                self.assertNotIn(b'E2E Camera', payload)

    def test_login_rate_limit_and_csrf_are_real_policy(self):
        bad = HarnessClient(self.base)
        self.assertEqual(
            bad.json('POST', '/api/login', body={'password': 'wrong'})[0], 401)
        client = self.login()
        # A state-changing PATCH without CSRF is refused by the real core.
        status, _headers, _payload = client.call(
            'PATCH', '/api/settings', body={'field': 'snapshot_interval', 'value': 90})
        self.assertEqual(status, 403)

    def test_turn_lock_rejection_returns_authoritative_state(self):
        client = self.login()
        status, _headers, payload = client.json(
            'PATCH', '/api/settings',
            body={'field': 'quality', 'value': 'fhd'}, csrf=True)
        self.assertEqual(status, 200)
        self.assertFalse(payload['ok'])
        self.assertIn('TURN', payload['error'])
        self.assertEqual(payload['settings']['quality_tier'], 2)
        status, _headers, accepted = client.json(
            'PATCH', '/api/settings',
            body={'field': 'quality', 'value': 'sd'}, csrf=True)
        self.assertTrue(accepted['ok'])
        self.assertEqual(accepted['settings']['quality_tier'], 1)

    def test_live_frame_is_a_valid_jpeg_and_media_never_lists_the_index(self):
        client = self.login()
        status, headers, payload = client.call('GET', '/api/live/frame')
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('X-Live-State'), 'live')
        import live_monitor
        self.assertTrue(live_monitor.valid_jpeg(payload))

        status, _headers, videos = client.json(
            'GET', '/api/media/timelapses?page=1&page_size=10')
        self.assertEqual(status, 200)
        self.assertEqual(videos['total'], e2e_server.E2E_VIDEO_COUNT)
        names = [item['name'] for item in videos['items']]
        self.assertNotIn('.timelapse_videos.csv', names)

    def test_update_check_is_report_only_and_install_needs_reauth(self):
        client = self.login()
        status, _headers, payload = client.json(
            'POST', '/api/update/check', body={}, csrf=True)
        self.assertEqual(status, 202)
        self.assertTrue(payload['checking'])
        status, _headers, _payload = client.call(
            'POST', '/api/update/install', body={}, csrf=True)
        self.assertEqual(status, 403)

    def test_read_routes_trigger_no_destructive_action(self):
        client = self.login()
        for path in ('/api/dashboard', '/api/system', '/api/update',
                     '/api/integrations', '/api/diagnostics'):
            client.call('GET', path)
        status, _headers, payload = client.json('GET', '/__e2e/counters')
        counters = payload['counters']
        for name in ('reboot', 'ssh', 'recovery', 'reset',
                     'update_check', 'update_install'):
            self.assertEqual(counters[name], 0, name)

    def test_network_gpio_and_session_fakes_follow_the_real_contracts(self):
        client = self.login()
        status, _h, pins = client.json('GET', '/api/gpio/pins')
        self.assertEqual(status, 200)
        self.assertIn(17, [pin['bcm'] for pin in pins['pins']])
        self.assertFalse(set(range(0, 4)) & {pin['bcm'] for pin in pins['pins']})
        self.assertFalse(pins['status']['armed'])

        # GPIO trigger: the coordinator rules the console relies on.
        def patch(field, value):
            return client.json('PATCH', '/api/settings',
                               body={'field': field, 'value': value}, csrf=True)[2]

        self.assertFalse(patch('timelapse_trigger', 'gpio')['ok'])    # no layer pin yet
        self.assertTrue(patch('timelapse_gpio_pin', 17)['ok'])
        self.assertFalse(patch('timelapse_gpio_record_pin', 17)['ok'])
        self.assertTrue(patch('timelapse_gpio_record_pin', 27)['ok'])
        self.assertTrue(patch('timelapse_trigger', 'gpio')['ok'])
        self.assertFalse(patch('timelapse_gpio_pin', None)['ok'])
        status, _h, pins = client.json('GET', '/api/gpio/pins')
        self.assertTrue(pins['status']['armed'])

        status, _h, sessions = client.json('GET', '/api/media/sessions')
        self.assertEqual([s['name'] for s in sessions['sessions']],
                         ['session_20260102-000000', 'session_20260101-000000'])
        status, _h, frames = client.json(
            'GET', '/api/media/frames?session=session_20260101-000000')
        self.assertEqual(frames['total'], 3)

        status, _h, network = client.json('GET', '/api/network')
        self.assertEqual(network['link']['ssid'], 'E2E-WiFi')
        self.assertTrue(network['psk_set'])
        self.assertNotIn('"psk"', json.dumps(network))

    def test_network_apply_needs_the_reauth_window_and_never_echoes_the_psk(self):
        client = self.login()
        body = {'ssid': 'Other', 'psk': 'e2e-secret-psk', 'ipv4_method': 'auto',
                'confirm': True}
        status, _h, _p = client.call('PUT', '/api/network', body=body, csrf=True)
        self.assertEqual(status, 403)
        client.json('POST', '/api/reauth', body={'password': e2e_server.E2E_ADMIN_PASSWORD},
                    csrf=True)
        status, _h, payload = client.call('PUT', '/api/network', body=body, csrf=True)
        self.assertEqual(status, 202)
        self.assertNotIn(b'e2e-secret-psk', payload)
        status, _h, first = client.json('GET', '/api/network')
        self.assertEqual(first['result']['state'], 'applying')
        status, _h, second = client.json('GET', '/api/network')
        self.assertEqual(second['result']['state'], 'applied')
        self.assertEqual(second['link']['ssid'], 'Other')

    def test_reset_restores_pristine_state(self):
        client = self.login()
        client.json('PATCH', '/api/settings',
                    body={'field': 'quality', 'value': 'sd'}, csrf=True)
        status, _headers, _payload = HarnessClient(self.base).call(
            'POST', '/__e2e/reset', body={})
        self.assertEqual(status, 200)
        fresh = self.login()
        status, _headers, payload = fresh.json('GET', '/api/dashboard')
        self.assertEqual(payload['settings']['quality_tier'], 2)


if __name__ == '__main__':
    unittest.main()
