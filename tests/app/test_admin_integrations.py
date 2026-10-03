"""WP-UI4 (AC-8/AC-9/AC-17/AC-18): integration config API.

Stdlib-only: the stdlib admin core with injected dashboard/mqtt-test providers
and ``tempfile`` config paths. No aiohttp, no socket, no ``/data``, no broker.
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import admin_auth  # noqa: E402
import admin_http  # noqa: E402
import config_schema  # noqa: E402
import provisioning  # noqa: E402

ADMIN_PASSWORD = 'correct horse battery staple'
ADMIN_HASH = admin_auth.hash_password(ADMIN_PASSWORD)
SESSION_COOKIE = admin_auth.SESSION_COOKIE_NAME
NOW = 1000.0

CANARY_TOKEN = 'CANARY-PRUSA-TOKEN-abcdef123456'
CANARY_MQTT_USER = 'CANARY-MQTT-USER-abcdef123456'
CANARY_MQTT_PASS = 'CANARY-MQTT-PASS-abcdef123456'
CANARY_PSK = 'CANARY-WIFI-PSK-abcdef123456'
CANARY_URI_USERINFO = 'CANARY-URI-USERINFO-abcdef123456'
CANARY_PERSONAL_PATH = '/home/secretuser/private/ca.pem'
FINGERPRINT = 'fp-seed-abcdef123456'


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


class IntegrationApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.device_path = self.root / 'device.toml'
        self.secrets_path = self.root / 'secrets.toml'
        self.sessions = admin_auth.SessionStore()
        self.limiter = admin_auth.LoginRateLimiter(
            max_attempts=3, window=300.0, lockout=60.0
        )
        self.provider = lambda: {
            'ok': True,
            'runtime': {'source': 'live', 'fresh': True},
            'prusa': {'state': 'authenticated'},
            'mqtt': {'state': 'connected', 'configured': True},
        }

    def _build_app(self, **overrides):
        kwargs = dict(
            mode='admin',
            sessions=self.sessions,
            limiter=self.limiter,
            admin_hash=ADMIN_HASH,
            provisioning_state=provisioning.ProvisioningState(state='claimed'),
            device_path=str(self.device_path),
            secrets_path=str(self.secrets_path),
            provisioning_path=str(self.root / 'provisioning.json'),
            clock=lambda: NOW,
            dashboard_provider=self.provider,
        )
        kwargs.update(overrides)
        return admin_http.AdminApp(**kwargs)

    def _seed(self):
        device = config_schema.default_device()
        device['camera_name'] = 'Bench'
        device['fingerprint'] = FINGERPRINT
        device['prusa']['server'] = 'connect.prusa3d.com'
        device['mqtt'].update({
            'enabled': True,
            'uri': 'mqtts://broker.example:8883',
            'client_id': 'bench-client',
            'discovery_prefix': 'homeassistant',
            'topic_prefix': 'pibuddycam',
            'ca_file': '/data/pibuddycam/config/ca.pem',
        })
        secrets = {
            'prusa': {'token': CANARY_TOKEN},
            'mqtt': {'username': CANARY_MQTT_USER, 'password': CANARY_MQTT_PASS},
            'wifi': {'psk': CANARY_PSK},
        }
        self.assertTrue(config_schema.save_device(device, path=str(self.device_path)))
        self.assertTrue(config_schema.save_secrets(secrets, path=str(self.secrets_path)))
        return device, secrets

    def _login(self, app):
        response = app.handle(_make_request(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD}))
        self.assertEqual(response.status, 200, response.body)
        return _parse_cookie(response), json.loads(response.body)['csrf']

    def _headers(self, token, csrf=None):
        headers = {'Cookie': f'{SESSION_COOKIE}={token}'}
        if csrf is not None:
            headers['X-CSRF-Token'] = csrf
        return headers

    def _reauth(self, app, token, csrf):
        response = app.handle(_make_request(
            'POST', '/api/reauth', body={'password': ADMIN_PASSWORD},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 200, response.body)

    # -- GET -----------------------------------------------------------------

    def test_get_requires_authentication(self):
        app = self._build_app()
        response = app.handle(_make_request('GET', '/api/integrations'))
        self.assertEqual(response.status, 401)

    def test_get_returns_redacted_configuration(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        response = app.handle(_make_request(
            'GET', '/api/integrations', headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload['ok'])
        self.assertTrue(payload['prusa']['token_configured'])
        self.assertTrue(payload['prusa']['fingerprint_configured'])
        self.assertNotIn('token', payload['prusa'])
        self.assertTrue(payload['mqtt']['username_configured'])
        self.assertTrue(payload['mqtt']['password_configured'])
        self.assertTrue(payload['mqtt']['ca_file_configured'])
        self.assertEqual(payload['mqtt']['uri'], 'mqtts://broker.example:8883')
        self.assertIsNotNone(payload['mqtt']['effective_topics'])
        self.assertIn('state', payload['mqtt']['effective_topics'])
        self.assertEqual(payload['runtime']['source'], 'live')

    def test_get_excludes_every_planted_secret(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        body = app.handle(_make_request(
            'GET', '/api/integrations', headers=self._headers(token, csrf))
        ).body.decode('utf-8')
        for canary in (CANARY_TOKEN, CANARY_MQTT_USER, CANARY_MQTT_PASS, CANARY_PSK):
            self.assertNotIn(canary, body, canary)
        self.assertNotIn(token, body)
        self.assertNotIn(ADMIN_HASH, body)

    def test_get_reports_unavailable_runtime_honestly(self):
        self._seed()
        app = self._build_app(dashboard_provider=None)
        token, csrf = self._login(app)
        payload = json.loads(app.handle(_make_request(
            'GET', '/api/integrations', headers=self._headers(token, csrf))).body)
        self.assertEqual(payload['runtime']['source'], 'unavailable')
        self.assertFalse(payload['runtime']['fresh'])

    # -- PUT MQTT ------------------------------------------------------------

    def test_mqtt_put_requires_auth_csrf_and_reauth(self):
        self._seed()
        app = self._build_app()
        body = {'enabled': False, 'uri': 'mqtts://new.example:8883'}
        self.assertEqual(
            app.handle(_make_request('PUT', '/api/integrations/mqtt', body=body)).status,
            401)
        token, csrf = self._login(app)
        # No CSRF -> 403.
        self.assertEqual(
            app.handle(_make_request(
                'PUT', '/api/integrations/mqtt', body=body,
                headers=self._headers(token))).status,
            403)
        # CSRF but no fresh re-auth -> 403.
        self.assertEqual(
            app.handle(_make_request(
                'PUT', '/api/integrations/mqtt', body=body,
                headers=self._headers(token, csrf))).status,
            403)

    def test_mqtt_put_broker_password_is_never_an_admin_password(self):
        # The MQTT body's ``password`` is the broker secret. Without a fresh
        # re-auth window it must be refused outright -- even when its value
        # happens to equal the admin password -- and must not charge the login
        # limiter (the original credential-name collision defect).
        self._seed()
        before = self.device_path.read_bytes()
        app = self._build_app()
        token, csrf = self._login(app)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/mqtt',
            body={'enabled': True, 'uri': 'mqtts://broker.example:8883',
                  'password': ADMIN_PASSWORD},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 403, response.body)
        self.assertEqual(self.device_path.read_bytes(), before)
        self.assertEqual(self.limiter.count(), 0)
        self.assertNotIn(ADMIN_PASSWORD, response.body.decode('utf-8'))

    def test_mqtt_put_missing_reauth_never_locks_the_limiter(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        for _ in range(6):
            response = app.handle(_make_request(
                'PUT', '/api/integrations/mqtt',
                body={'enabled': True, 'uri': 'mqtts://broker.example:8883',
                      'password': 'broker-password-guess'},
                headers=self._headers(token, csrf), peer_ip='10.9.9.9'))
            self.assertEqual(response.status, 403, response.body)
        # The broker password was never recorded as a failed admin login ...
        self.assertEqual(self.limiter.count(), 0)
        # ... so the peer is not locked out of a genuine login attempt.
        wrong = app.handle(_make_request(
            'POST', '/api/login', body={'password': 'wrong'},
            peer_ip='10.9.9.9'))
        self.assertEqual(wrong.status, 401)

    def test_mqtt_put_fresh_reauth_accepts_body_password_as_broker_secret(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/mqtt',
            body={'enabled': True, 'uri': 'mqtts://broker.example:8883',
                  'password': 'new-broker-secret'},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 200, response.body)
        secrets = config_schema.load_secrets(str(self.secrets_path))
        self.assertEqual(secrets['mqtt']['password'], 'new-broker-secret')

    # -- POST /api/mqtt/test (stored credentials) ---------------------------

    def test_mqtt_test_uses_stored_credentials_only_when_authenticated(self):
        self._seed()
        seen = []

        def probe(config):
            seen.append(config)
            return True, 'connected'

        app = self._build_app(mqtt_probe=probe)
        token, csrf = self._login(app)
        response = app.handle(_make_request(
            'POST', '/api/mqtt/test',
            body={'uri': 'mqtts://broker.example:8883',
                  'use_stored_username': True, 'use_stored_password': True},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 200, response.body)
        self.assertTrue(json.loads(response.body)['ok'])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].username, CANARY_MQTT_USER)
        self.assertEqual(seen[0].password, CANARY_MQTT_PASS)
        body = response.body.decode('utf-8')
        for canary in (CANARY_MQTT_USER, CANARY_MQTT_PASS):
            self.assertNotIn(canary, body, canary)

    def test_mqtt_test_stored_flag_refused_in_setup_mode(self):
        self._seed()
        seen = []
        app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
            mqtt_probe=lambda config: (seen.append(config), (True, 'connected'))[1],
        )
        response = app.handle(_make_request(
            'POST', '/api/mqtt/test',
            body={'uri': 'mqtts://broker.example:8883',
                  'use_stored_password': True}))
        self.assertEqual(response.status, 403, response.body)
        self.assertEqual(seen, [])

    def test_mqtt_test_stored_flag_requires_session_post_claim(self):
        self._seed()
        app = self._build_app(mqtt_probe=lambda config: (True, 'connected'))
        response = app.handle(_make_request(
            'POST', '/api/mqtt/test',
            body={'uri': 'mqtts://broker.example:8883',
                  'use_stored_password': True}))
        self.assertEqual(response.status, 401)

    def test_mqtt_test_stored_credential_missing_is_rejected(self):
        device = config_schema.default_device()
        device['fingerprint'] = FINGERPRINT
        config_schema.save_device(device, path=str(self.device_path))
        config_schema.save_secrets(
            {'wifi': {'psk': CANARY_PSK}}, path=str(self.secrets_path))
        app = self._build_app(mqtt_probe=lambda config: (True, 'connected'))
        token, csrf = self._login(app)
        response = app.handle(_make_request(
            'POST', '/api/mqtt/test',
            body={'uri': 'mqtts://broker.example:8883',
                  'use_stored_password': True},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 400, response.body)

    def test_mqtt_test_stored_credential_redacted_from_reason_and_logs(self):
        self._seed()

        def probe(config):
            return False, f'refused for {config.username}:{config.password}'

        app = self._build_app(mqtt_probe=probe)
        token, csrf = self._login(app)
        with self.assertLogs('pibuddycam.admin_http', level='DEBUG') as logs:
            response = app.handle(_make_request(
                'POST', '/api/mqtt/test',
                body={'uri': 'mqtts://broker.example:8883',
                      'use_stored_username': True, 'use_stored_password': True},
                headers=self._headers(token, csrf)))
        body = response.body.decode('utf-8')
        recorded = '\n'.join(logs.output)
        for canary in (CANARY_MQTT_USER, CANARY_MQTT_PASS):
            self.assertNotIn(canary, body, canary)
            self.assertNotIn(canary, recorded, canary)

    def test_mqtt_test_submitted_secret_redacted_from_response_and_logs(self):
        self._seed()

        def probe(config):
            return False, f'refused for {CANARY_MQTT_PASS}'

        app = self._build_app(mqtt_probe=probe)
        token, csrf = self._login(app)
        with self.assertLogs('pibuddycam.admin_http', level='DEBUG') as logs:
            response = app.handle(_make_request(
                'POST', '/api/mqtt/test',
                body={'uri': 'mqtts://broker.example:8883',
                      'username': 'submitted-user', 'password': CANARY_MQTT_PASS},
                headers=self._headers(token, csrf)))
        body = response.body.decode('utf-8')
        recorded = '\n'.join(logs.output)
        self.assertNotIn(CANARY_MQTT_PASS, body)
        self.assertNotIn(CANARY_MQTT_PASS, recorded)

    def test_mqtt_test_stored_flag_wrong_type_rejected(self):
        self._seed()
        app = self._build_app(mqtt_probe=lambda config: (True, 'connected'))
        token, csrf = self._login(app)
        response = app.handle(_make_request(
            'POST', '/api/mqtt/test',
            body={'uri': 'mqtts://broker.example:8883', 'use_stored_password': 'yes'},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 400)

    def test_mqtt_put_saves_atomically_with_modes(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/mqtt',
            body={
                'enabled': True,
                'uri': 'mqtts://broker2.example:8883',
                'client_id': 'new-client',
                'discovery_prefix': 'ha',
                'topic_prefix': 'cam',
                'ca_file': '',
            },
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 200, response.body)
        payload = json.loads(response.body)
        self.assertTrue(payload['ok'])
        self.assertTrue(payload['restart_required'])
        self.assertFalse(payload['active'])
        device = config_schema.load_device(str(self.device_path))
        self.assertEqual(device['mqtt']['uri'], 'mqtts://broker2.example:8883')
        self.assertEqual(device['mqtt']['client_id'], 'new-client')
        self.assertEqual(device['camera_name'], 'Bench')  # untouched
        self.assertEqual(stat.S_IMODE(os.stat(self.device_path).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(self.secrets_path).st_mode), 0o600)
        self.assertFalse((self.root / 'state.json').exists())

    def test_mqtt_blank_secret_keeps_existing(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/mqtt',
            body={'enabled': True, 'uri': 'mqtts://broker.example:8883',
                  'username': '', 'password': ''},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 200)
        secrets = config_schema.load_secrets(str(self.secrets_path))
        self.assertEqual(secrets['mqtt']['username'], CANARY_MQTT_USER)
        self.assertEqual(secrets['mqtt']['password'], CANARY_MQTT_PASS)

    def test_mqtt_explicit_clear_removes_secret(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/mqtt',
            body={'enabled': True, 'uri': 'mqtts://broker.example:8883',
                  'clear_username': True, 'clear_password': True},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 200)
        secrets = config_schema.load_secrets(str(self.secrets_path))
        self.assertNotIn('username', secrets.get('mqtt', {}))
        self.assertNotIn('password', secrets.get('mqtt', {}))

    def test_mqtt_put_replacement_writes_new_secret(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/mqtt',
            body={'enabled': True, 'uri': 'mqtts://broker.example:8883',
                  'username': 'new-user', 'password': 'new-pass'},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 200)
        secrets = config_schema.load_secrets(str(self.secrets_path))
        self.assertEqual(secrets['mqtt']['username'], 'new-user')
        self.assertEqual(secrets['mqtt']['password'], 'new-pass')

    def test_mqtt_uri_userinfo_rejected_without_write(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/mqtt',
            body={'enabled': True, 'uri': 'mqtts://user:pass@broker.example:8883'},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 400)
        self.assertNotIn('user:pass', response.body.decode('utf-8'))
        device = config_schema.load_device(str(self.device_path))
        self.assertEqual(device['mqtt']['uri'], 'mqtts://broker.example:8883')

    def test_mqtt_unknown_field_rejected_without_write(self):
        self._seed()
        before = self.device_path.read_bytes()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/mqtt',
            body={'enabled': True, 'uri': 'mqtts://broker.example:8883', 'command': 'rm'},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 400)
        self.assertEqual(self.device_path.read_bytes(), before)

    def test_mqtt_wrong_type_rejected_without_write(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        for body in (
            {'enabled': 'yes', 'uri': 'mqtts://broker.example:8883'},
            {'enabled': True, 'uri': {'nested': True}},
            {'enabled': True, 'uri': 'mqtts://broker.example:8883', 'clear_password': 'yes'},
        ):
            with self.subTest(body=body):
                response = app.handle(_make_request(
                    'PUT', '/api/integrations/mqtt', body=body,
                    headers=self._headers(token, csrf)))
                self.assertEqual(response.status, 400)

    def test_mqtt_oversize_rejected_without_write(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/mqtt',
            body={'enabled': True, 'uri': 'mqtts://broker.example:8883',
                  'client_id': 'x' * 20000},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 413)

    def test_mqtt_submitted_secret_not_echoed(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/mqtt',
            body={'enabled': True, 'uri': 'mqtts://user:pass@broker.example:8883',
                  'username': CANARY_MQTT_USER, 'password': CANARY_MQTT_PASS},
            headers=self._headers(token, csrf)))
        body = response.body.decode('utf-8')
        for canary in (CANARY_MQTT_USER, CANARY_MQTT_PASS, 'user:pass'):
            self.assertNotIn(canary, body, canary)

    # -- PUT Prusa -----------------------------------------------------------

    def test_prusa_put_requires_reauth(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/prusa',
            body={'server': 'connect.prusa3d.com', 'token': 'new'},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 403)

    def test_prusa_blank_token_and_fingerprint_keep(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/prusa',
            body={'server': 'connect.prusa3d.com', 'token': '', 'fingerprint': ''},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 200)
        secrets = config_schema.load_secrets(str(self.secrets_path))
        self.assertEqual(secrets['prusa']['token'], CANARY_TOKEN)
        device = config_schema.load_device(str(self.device_path))
        self.assertEqual(device['fingerprint'], FINGERPRINT)

    def test_prusa_token_replacement_and_fingerprint_warning(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/prusa',
            body={'server': 'connect.prusa3d.com',
                  'token': 'new-token-value', 'fingerprint': 'new-fingerprint'},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload['restart_required'])
        self.assertFalse(payload['active'])
        self.assertTrue(any('fingerprint' in w for w in payload['warnings']))
        secrets = config_schema.load_secrets(str(self.secrets_path))
        self.assertEqual(secrets['prusa']['token'], 'new-token-value')
        self.assertEqual(stat.S_IMODE(os.stat(self.secrets_path).st_mode), 0o600)

    def _put_new_token(self, app, **extra):
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        body = {'server': 'connect.prusa3d.com', 'token': 'new-token-value'}
        body.update(extra)
        return app.handle(_make_request(
            'PUT', '/api/integrations/prusa', body=body,
            headers=self._headers(token, csrf)))

    def test_prusa_new_token_restarts_the_camera_and_is_active(self):
        self._seed()
        calls = []
        app = self._build_app(camera_restart_fn=lambda: calls.append('restart') or True)
        response = self._put_new_token(app)
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertEqual(calls, ['restart'])
        self.assertTrue(payload['active'])
        self.assertFalse(payload['restart_required'])
        self.assertEqual(payload['warnings'], [])
        secrets = config_schema.load_secrets(str(self.secrets_path))
        self.assertEqual(secrets['prusa']['token'], 'new-token-value')

    def test_prusa_failed_restart_keeps_restart_required(self):
        # An older image's helper lacks the verb: saved, but the user must restart.
        self._seed()
        app = self._build_app(camera_restart_fn=lambda: False)
        payload = json.loads(self._put_new_token(app).body)
        self.assertFalse(payload['active'])
        self.assertTrue(payload['restart_required'])
        self.assertTrue(payload['warnings'])

    def test_prusa_restart_error_does_not_fail_the_save(self):
        self._seed()

        def boom():
            raise RuntimeError('systemctl exploded')

        app = self._build_app(camera_restart_fn=boom)
        response = self._put_new_token(app)
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(response.body)['restart_required'])
        secrets = config_schema.load_secrets(str(self.secrets_path))
        self.assertEqual(secrets['prusa']['token'], 'new-token-value')

    def test_prusa_save_without_a_new_token_does_not_restart(self):
        self._seed()
        calls = []
        app = self._build_app(camera_restart_fn=lambda: calls.append('restart') or True)
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/prusa',
            body={'server': 'connect.prusa3d.com', 'token': ''},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 200)
        self.assertEqual(calls, [])
        self.assertTrue(json.loads(response.body)['restart_required'])

    def test_prusa_server_change_restarts_the_camera(self):
        self._seed()
        calls = []
        app = self._build_app(camera_restart_fn=lambda: calls.append('restart') or True)
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/prusa',
            body={'server': 'camera.example.org', 'token': ''},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertEqual(calls, ['restart'])
        self.assertTrue(payload['active'])
        self.assertFalse(payload['restart_required'])
        self.assertEqual(
            config_schema.load_device(str(self.device_path))['prusa']['server'],
            'camera.example.org')

    def test_prusa_legacy_server_is_stored_as_the_working_host(self):
        self._seed()
        calls = []
        app = self._build_app(camera_restart_fn=lambda: calls.append('restart') or True)
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/prusa',
            body={'server': 'webcam.connect.prusa3d.com', 'token': ''},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 200)
        # The seeded server already is the working host: nothing changed.
        self.assertEqual(calls, [])
        self.assertEqual(
            config_schema.load_device(str(self.device_path))['prusa']['server'],
            'connect.prusa3d.com')

    def test_prusa_clearing_the_token_restarts_the_camera(self):
        self._seed()
        calls = []
        app = self._build_app(camera_restart_fn=lambda: calls.append('restart') or True)
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/prusa',
            body={'clear_token': True},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 200)
        self.assertEqual(calls, ['restart'])

    def test_prusa_explicit_clear_removes_token_and_fingerprint(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/prusa',
            body={'server': 'connect.prusa3d.com',
                  'clear_token': True, 'clear_fingerprint': True},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 200)
        secrets = config_schema.load_secrets(str(self.secrets_path))
        self.assertNotIn('prusa', secrets)
        device = config_schema.load_device(str(self.device_path))
        self.assertEqual(device['fingerprint'], '')

    def test_prusa_never_returns_token(self):
        self._seed()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/prusa',
            body={'server': 'connect.prusa3d.com', 'token': 'new-token-value'},
            headers=self._headers(token, csrf)))
        body = response.body.decode('utf-8')
        self.assertNotIn('new-token-value', body)
        self.assertNotIn(CANARY_TOKEN, body)

    def test_prusa_empty_server_rejected_without_write(self):
        self._seed()
        before = self.device_path.read_bytes()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/prusa',
            body={'server': '   ', 'token': 'new'},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 400)
        self.assertEqual(self.device_path.read_bytes(), before)

    def test_prusa_unknown_field_rejected_without_write(self):
        self._seed()
        before = self.secrets_path.read_bytes()
        app = self._build_app()
        token, csrf = self._login(app)
        self._reauth(app, token, csrf)
        response = app.handle(_make_request(
            'PUT', '/api/integrations/prusa',
            body={'server': 'connect.prusa3d.com', 'token': 'x', 'token_file': '/etc/passwd'},
            headers=self._headers(token, csrf)))
        self.assertEqual(response.status, 400)
        self.assertEqual(self.secrets_path.read_bytes(), before)


class IntegrationViewUnitTests(unittest.TestCase):
    """Direct checks on the redaction helpers (AC-8/AC-17)."""

    def test_redacted_uri_strips_userinfo(self):
        uri = admin_http._redacted_uri(
            f'mqtts://{CANARY_URI_USERINFO}:secret@broker.example:8883/path')
        self.assertEqual(uri, 'mqtts://broker.example:8883/path')
        self.assertNotIn(CANARY_URI_USERINFO, uri)
        self.assertNotIn('secret', uri)

    def test_redacted_uri_passthrough_and_malformed(self):
        self.assertEqual(
            admin_http._redacted_uri('mqtts://broker.example:8883'),
            'mqtts://broker.example:8883')
        self.assertEqual(admin_http._redacted_uri(None), '')
        self.assertEqual(admin_http._redacted_uri(123), '')

    def test_mqtt_view_never_returns_secret_values(self):
        device = config_schema.default_device()
        device['fingerprint'] = FINGERPRINT
        device['mqtt']['uri'] = (
            f'mqtts://{CANARY_URI_USERINFO}:secret@broker.example:8883')
        device['mqtt']['ca_file'] = CANARY_PERSONAL_PATH
        secrets = {'mqtt': {'username': CANARY_MQTT_USER, 'password': CANARY_MQTT_PASS}}
        view = admin_http._mqtt_integration_view(device, secrets)
        blob = json.dumps(view)
        for canary in (CANARY_MQTT_USER, CANARY_MQTT_PASS, CANARY_URI_USERINFO,
                       CANARY_PERSONAL_PATH):
            self.assertNotIn(canary, blob, canary)
        self.assertFalse(view['ca_file_configured'] is None)
        self.assertTrue(view['username_configured'])
        self.assertTrue(view['password_configured'])

    def test_prusa_view_never_returns_token_or_fingerprint(self):
        device = config_schema.default_device()
        device['fingerprint'] = FINGERPRINT
        secrets = {'prusa': {'token': CANARY_TOKEN}}
        view = admin_http._prusa_integration_view(device, secrets)
        blob = json.dumps(view)
        self.assertNotIn(CANARY_TOKEN, blob)
        self.assertNotIn(FINGERPRINT, blob)
        self.assertTrue(view['token_configured'])
        self.assertTrue(view['fingerprint_configured'])


if __name__ == '__main__':
    unittest.main()
