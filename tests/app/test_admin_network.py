"""Console network API: auth, CSRF, re-auth window, bounds, redaction, 501."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import admin_auth  # noqa: E402
import admin_http  # noqa: E402
import config_schema  # noqa: E402
import network_settings  # noqa: E402
import privileged  # noqa: E402
import provisioning  # noqa: E402

ADMIN_PASSWORD = 'correct horse battery staple'
ADMIN_HASH = admin_auth.hash_password(ADMIN_PASSWORD)
SESSION_COOKIE = admin_auth.SESSION_COOKIE_NAME
NOW = 1000.0
PSK = 'CANARY-WIFI-PSK-abcdef123456'


def make_request(method, path, body=None, headers=None, query=None, now=NOW):
    raw = b''
    merged = {}
    if isinstance(body, dict):
        raw = json.dumps(body).encode('utf-8')
        merged['Content-Type'] = 'application/json'
    elif isinstance(body, bytes):
        raw = body
    merged.update(headers or {})
    return admin_http.Request(method, path, query or {}, merged, raw, '10.0.0.5', now)


class FakeController:
    """Records calls; behaves like NetworkController for the API layer."""

    def __init__(self):
        self.applied = []
        self.hostnames = []
        self.ntp = []
        self.apply_status = 202
        self.status_doc = {
            'link': {'connected': True, 'ssid': 'Home', 'address': '192.0.2.10',
                     'prefix': 24, 'gateway': '192.0.2.1', 'dns': [], 'ipv4': {'method': 'auto'}},
            'hostname': 'cam', 'ntp_servers': [], 'psk_set': True,
            'time': {'synchronized': True, 'server': 'x', 'now': 1},
            'result': None, 'busy': False,
        }

    def status(self):
        return dict(self.status_doc)

    def current_address(self):
        return '192.0.2.10'

    def scan(self):
        return 200, {'scanning': False, 'networks': [
            {'ssid': 'Home', 'signal': 80, 'secured': True}]}

    def apply(self, request):
        self.applied.append(request)
        if self.apply_status == 202:
            return 202, {'accepted': True, 'expected_address': request.get('address', '')}
        return self.apply_status, {'error': 'nope'}

    def set_hostname(self, name):
        self.hostnames.append(name)
        return 200, {'ok': True, 'hostname': name, 'reboot_recommended': True}

    def set_ntp_servers(self, servers):
        self.ntp.append(servers)
        return 200, {'ok': True, 'ntp_servers': servers, 'applied': True}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.controller = FakeController()

    def build(self, controller='default'):
        return admin_http.AdminApp(
            mode='admin',
            sessions=admin_auth.SessionStore(),
            limiter=admin_auth.LoginRateLimiter(max_attempts=3, window=300.0, lockout=60.0),
            admin_hash=ADMIN_HASH,
            provisioning_state=provisioning.ProvisioningState(state='claimed'),
            device_path=str(self.root / 'device.toml'),
            secrets_path=str(self.root / 'secrets.toml'),
            provisioning_path=str(self.root / 'provisioning.json'),
            clock=lambda: NOW,
            network_controller=self.controller if controller == 'default' else controller,
            dashboard_provider=lambda: {
                'ok': True, 'timelapse_gpio': {'armed': True, 'shot_pin': 17}},
        )

    def login(self, app):
        response = app.handle(make_request(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD}))
        self.assertEqual(response.status, 200)
        cookie = ''
        for part in response.headers['Set-Cookie'].split(';'):
            name, _, value = part.strip().partition('=')
            if name == SESSION_COOKIE:
                cookie = value
        return cookie, json.loads(response.body)['csrf']

    def headers(self, token, csrf=None):
        headers = {'Cookie': f'{SESSION_COOKIE}={token}'}
        if csrf:
            headers['X-CSRF-Token'] = csrf
        return headers

    def reauth(self, app, token, csrf):
        response = app.handle(make_request(
            'POST', '/api/reauth', body={'password': ADMIN_PASSWORD},
            headers=self.headers(token, csrf)))
        self.assertEqual(response.status, 200)

    def session(self, app, reauth=True):
        token, csrf = self.login(app)
        if reauth:
            self.reauth(app, token, csrf)
        return self.headers(token, csrf)


VALID = {'ssid': 'Home', 'psk': PSK, 'ipv4_method': 'auto', 'confirm': True}


class ReadTests(Base):
    def test_requires_authentication(self):
        app = self.build()
        for path in ('/api/network', '/api/network/scan', '/api/gpio/pins'):
            self.assertEqual(app.handle(make_request('GET', path)).status, 401, path)

    def test_status_never_contains_the_psk(self):
        app = self.build()
        headers = self.session(app, reauth=False)
        response = app.handle(make_request('GET', '/api/network', headers=headers))
        self.assertEqual(response.status, 200)
        body = json.loads(response.body)
        self.assertTrue(body['ok'])
        self.assertTrue(body['psk_set'])
        self.assertNotIn('psk"', response.body.decode())
        self.assertEqual(body['link']['ssid'], 'Home')

    def test_scan(self):
        app = self.build()
        headers = self.session(app, reauth=False)
        response = app.handle(make_request('GET', '/api/network/scan', headers=headers))
        self.assertEqual(json.loads(response.body)['networks'][0]['ssid'], 'Home')

    def test_501_without_a_controller(self):
        app = self.build(controller=None)
        headers = self.session(app, reauth=False)
        for method, path in (('GET', '/api/network'), ('GET', '/api/network/scan')):
            self.assertEqual(
                app.handle(make_request(method, path, headers=headers)).status, 501)

    def test_system_view_reports_the_address(self):
        app = self.build()
        headers = self.session(app, reauth=False)
        body = json.loads(app.handle(
            make_request('GET', '/api/system', headers=headers)).body)
        self.assertEqual(body['network']['address'], '192.0.2.10')

    def test_gpio_pins_lists_the_safe_table_and_status(self):
        app = self.build()
        headers = self.session(app, reauth=False)
        body = json.loads(app.handle(
            make_request('GET', '/api/gpio/pins', headers=headers)).body)
        bcms = [pin['bcm'] for pin in body['pins']]
        self.assertIn(17, bcms)
        for excluded in (0, 1, 2, 3, 7, 8, 9, 10, 11, 14, 15):
            self.assertNotIn(excluded, bcms)
        self.assertEqual(body['defaults'], {'shot': 17, 'record': 27})
        self.assertEqual(body['status']['shot_pin'], 17)
        self.assertIn('header pin 11', next(
            p['label'] for p in body['pins'] if p['bcm'] == 17))


class PutTests(Base):
    def put(self, app, headers, body, path='/api/network'):
        return app.handle(make_request('PUT', path, body=body, headers=headers))

    def test_unauthenticated_is_401(self):
        app = self.build()
        self.assertEqual(self.put(app, {}, VALID).status, 401)

    def test_missing_csrf_is_403(self):
        app = self.build()
        token, csrf = self.login(app)
        self.reauth(app, token, csrf)
        response = self.put(app, self.headers(token), VALID)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.controller.applied, [])

    def test_requires_the_fresh_reauth_window(self):
        app = self.build()
        headers = self.session(app, reauth=False)
        response = self.put(app, headers, VALID)
        self.assertIn(response.status, (401, 403))
        self.assertEqual(self.controller.applied, [])

    def test_accepted_with_202_and_redacts_the_psk(self):
        app = self.build()
        headers = self.session(app)
        response = self.put(app, headers, VALID)
        self.assertEqual(response.status, 202, response.body)
        self.assertNotIn(PSK, response.body.decode())
        body = json.loads(response.body)
        self.assertTrue(body['ok'] and body['accepted'])
        self.assertIn('warning', body)
        self.assertEqual(self.controller.applied[0]['ssid'], 'Home')
        self.assertEqual(self.controller.applied[0]['psk'], PSK)

    def test_static_request_reports_the_expected_address(self):
        app = self.build()
        headers = self.session(app)
        body = dict(VALID, ipv4_method='manual', address='192.0.2.50', prefix=24,
                    gateway='192.0.2.1', dns=['192.0.2.1'])
        response = self.put(app, headers, body)
        self.assertEqual(json.loads(response.body)['expected_address'], '192.0.2.50')

    def test_confirmation_is_required(self):
        app = self.build()
        headers = self.session(app)
        for body in (dict(VALID, confirm=False), {k: v for k, v in VALID.items() if k != 'confirm'},
                     dict(VALID, confirm='yes')):
            response = self.put(app, headers, body)
            self.assertEqual(response.status, 400)
            self.assertNotIn(PSK, response.body.decode())
        self.assertEqual(self.controller.applied, [])

    def test_unknown_field_type_and_validation_errors_are_400(self):
        app = self.build()
        headers = self.session(app)
        for body in (
            dict(VALID, extra=1), dict(VALID, ssid=''), dict(VALID, psk='short'),
            dict(VALID, ipv4_method='manual'),
            dict(VALID, ipv4_method='manual', address='192.0.2.5', prefix=24,
                 gateway='198.51.100.1'),
            ['not', 'a', 'dict'],
        ):
            response = self.put(app, headers, body)
            self.assertEqual(response.status, 400, body)
            self.assertNotIn(PSK, response.body.decode())
        self.assertEqual(self.controller.applied, [])

    def test_oversize_body_is_413(self):
        app = self.build()
        headers = self.session(app)
        response = self.put(app, headers, dict(VALID, ssid='x' * 40000))
        self.assertEqual(response.status, 413)
        self.assertEqual(self.controller.applied, [])

    def test_busy_is_409_and_helper_missing_is_501(self):
        app = self.build()
        headers = self.session(app)
        self.controller.apply_status = 409
        self.assertEqual(self.put(app, headers, VALID).status, 409)
        self.controller.apply_status = 501
        self.assertEqual(self.put(app, headers, VALID).status, 501)
        self.controller.apply_status = 502
        response = self.put(app, headers, VALID)
        self.assertEqual(response.status, 502)
        self.assertNotIn(PSK, response.body.decode())

    def test_501_without_a_controller(self):
        app = self.build(controller=None)
        headers = self.session(app)
        self.assertEqual(self.put(app, headers, VALID).status, 501)

    def test_hostname(self):
        app = self.build()
        headers = self.session(app)
        path = '/api/network/hostname'
        response = self.put(app, headers, {'hostname': 'Print-Cam'}, path)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.controller.hostnames, ['print-cam'])
        self.assertTrue(json.loads(response.body)['reboot_recommended'])
        for bad in ({'hostname': 'a b'}, {'hostname': ''}, {'hostname': '-x'},
                    {'hostname': 'ok', 'x': 1}, {'hostname': 5}):
            self.assertEqual(self.put(app, headers, bad, path).status, 400, bad)
        self.assertEqual(self.controller.hostnames, ['print-cam'])

    def test_hostname_needs_the_reauth_window(self):
        app = self.build()
        headers = self.session(app, reauth=False)
        response = self.put(app, headers, {'hostname': 'cam'}, '/api/network/hostname')
        self.assertIn(response.status, (401, 403))
        self.assertEqual(self.controller.hostnames, [])

    def test_ntp_servers(self):
        app = self.build()
        headers = self.session(app, reauth=False)
        path = '/api/network/ntp'
        response = self.put(app, headers, {'ntp_servers': ['time.example', '192.0.2.1']}, path)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.controller.ntp, [['time.example', '192.0.2.1']])
        response = self.put(app, headers, {'ntp_servers': []}, path)
        self.assertEqual(response.status, 200)
        for bad in ({'ntp_servers': ['a', 'b', 'c', 'd']}, {'ntp_servers': ['bad host']},
                    {'ntp_servers': 'x'}, {'ntp_servers': [], 'x': 1}):
            self.assertEqual(self.put(app, headers, bad, path).status, 400, bad)

    def test_ntp_requires_csrf(self):
        app = self.build()
        token, _csrf = self.login(app)
        response = self.put(app, self.headers(token), {'ntp_servers': []}, '/api/network/ntp')
        self.assertEqual(response.status, 403)


class EndToEndWithRealController(Base):
    """The real controller behind the real API: nothing but the helper is faked."""

    def test_psk_reaches_the_helper_only_as_stdin_json(self):
        calls = []

        def apply_fn(body):
            calls.append(body)
            return privileged.PrivilegedResult(True)

        config_schema.save_pair(
            config_schema.default_device(), {}, str(self.root / 'device.toml'),
            str(self.root / 'secrets.toml'))
        controller = network_settings.NetworkController(
            runner=lambda a, t, input=None: type('R', (), {'returncode': 1, 'stdout': ''})(),
            apply_fn=apply_fn, device_path=str(self.root / 'device.toml'),
            secrets_path=str(self.root / 'secrets.toml'),
            result_path=str(self.root / 'result.json'))
        app = self.build(controller=controller)
        headers = self.session(app)
        response = app.handle(make_request('PUT', '/api/network', body=VALID, headers=headers))
        self.assertEqual(response.status, 202)
        self.assertEqual(json.loads(calls[0])['psk'], PSK)
        self.assertNotIn(PSK, response.body.decode())
        second = app.handle(make_request('PUT', '/api/network', body=VALID, headers=headers))
        self.assertEqual(second.status, 409)


if __name__ == '__main__':
    unittest.main()
