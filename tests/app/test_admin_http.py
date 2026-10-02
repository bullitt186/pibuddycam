"""WP-3e1 AC-17/AC-19/AC-20: admin/provisioning HTTP core (admin_http).

Stdlib-only. No ``aiohttp``/``socketio``/``gi``/GStreamer import; every path is a
``tempfile`` path, the SSH runner and hotspot are fakes, and time is injected via
``Request.now`` so nothing sleeps. No real network, subprocess, or ``/data``.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import admin_auth  # noqa: E402
import admin_http  # noqa: E402
import config_schema  # noqa: E402
import device_control  # noqa: E402
import factory_reset  # noqa: E402
import privileged  # noqa: E402
import live_monitor  # noqa: E402
import provisioning  # noqa: E402
import setup_wizard  # noqa: E402

ADMIN_PASSWORD = 'correct horse battery staple'
ADMIN_HASH = admin_auth.hash_password(ADMIN_PASSWORD)
SESSION_COOKIE = admin_auth.SESSION_COOKIE_NAME
NOW = 1000.0


def _make_request(method, path, body=None, headers=None, peer_ip='10.0.0.5', now=NOW):
    """Build a transport-neutral :class:`admin_http.Request`."""
    if isinstance(body, (dict, list)):
        raw = json.dumps(body).encode('utf-8')
        merged = {'Content-Type': 'application/json'}
    elif isinstance(body, str):
        raw = body.encode('utf-8')
        merged = {}
    elif isinstance(body, bytes):
        raw = body
        merged = {}
    else:
        raw = b''
        merged = {}
    merged.update(headers or {})
    return admin_http.Request(method, path, {}, merged, raw, peer_ip, now)


def _parse_cookie(response):
    """Return the session cookie value from a response, or ``''``."""
    raw = response.headers.get('Set-Cookie', '')
    for part in raw.split(';'):
        name, _, value = part.strip().partition('=')
        if name == SESSION_COOKIE:
            return value
    return ''


class FakeRunner:
    """Records ``systemctl`` argv; never runs a process."""

    def __init__(self, returncode=0, stdout=''):
        self.returncode = returncode
        self.stdout = stdout
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(
            list(args), self.returncode, stdout=self.stdout, stderr=''
        )


class FakeHotspot:
    """Reports an active hotspot without touching NetworkManager."""

    def status(self):
        return SimpleNamespace(
            ok=True, active=True, ssid='PiBuddyCam-Setup-abc123',
            address='192.168.4.1', reason='',
        )

    def start(self, *args, **kwargs):
        return SimpleNamespace(ok=True, active=True, reason='')

    def stop(self, *args, **kwargs):
        return SimpleNamespace(ok=True, active=False, reason='')


class AdminHttpTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.sessions = admin_auth.SessionStore()
        self.limiter = admin_auth.LoginRateLimiter(
            max_attempts=3, window=300.0, lockout=60.0
        )
        self.ssh_runner = FakeRunner()
        self.hotspot = FakeHotspot()
        self.app = self._build_app()

    def _build_app(self, **overrides):
        kwargs = dict(
            mode='admin',
            sessions=self.sessions,
            limiter=self.limiter,
            admin_hash=ADMIN_HASH,
            provisioning_state=provisioning.ProvisioningState(state='claimed'),
            device_path=str(self.root / 'device.toml'),
            secrets_path=str(self.root / 'secrets.toml'),
            provisioning_path=str(self.root / 'provisioning.json'),
            recovery_path=str(self.root / 'pibuddycam-recovery'),
            ssh_runner=self.ssh_runner,
            clock=lambda: NOW,
        )
        kwargs.update(overrides)
        return admin_http.AdminApp(**kwargs)

    def req(self, method, path, body=None, headers=None, peer_ip='10.0.0.5', now=NOW):
        return _make_request(method, path, body, headers, peer_ip, now)

    def login(self, app=None, peer_ip='10.0.0.5', password=ADMIN_PASSWORD, now=NOW):
        app = app or self.app
        response = app.handle(self.req(
            'POST', '/api/login', body={'password': password},
            peer_ip=peer_ip, now=now,
        ))
        self.assertEqual(response.status, 200, response.body)
        return _parse_cookie(response)

    def auth_headers(self, token, csrf=None, extra=None):
        headers = {'Cookie': f'{SESSION_COOKIE}={token}'}
        if csrf is not None:
            headers['X-CSRF-Token'] = csrf
        headers.update(extra or {})
        return headers


# --------------------------------------------------------------------------- #
# Trusted-LAN labelling
# --------------------------------------------------------------------------- #

class TrustedLanTests(unittest.TestCase):
    def test_private_link_local_loopback_are_trusted(self):
        for peer_ip in (
            '10.0.0.1', '172.16.5.4', '172.31.255.254', '192.168.1.10',
            '169.254.3.4', '127.0.0.1', '::1', 'fe80::1',
        ):
            with self.subTest(peer_ip=peer_ip):
                self.assertTrue(admin_http.is_trusted_lan(peer_ip))

    def test_public_and_invalid_are_not_trusted(self):
        for peer_ip in (
            '8.8.8.8', '172.32.0.1', '192.169.0.1', '2001:db8::1',
            '', '   ', 'not-an-ip', None, 123,
        ):
            with self.subTest(peer_ip=peer_ip):
                self.assertFalse(admin_http.is_trusted_lan(peer_ip))

    def test_surrounding_whitespace_and_control_chars_rejected(self):
        for peer_ip in (
            ' 10.0.0.1', '10.0.0.1 ', '\t10.0.0.1', '10.0.0.1\n',
            '\x0010.0.0.1', '10.0.0.1\x00', '\u200b10.0.0.1',
        ):
            with self.subTest(peer_ip=peer_ip):
                self.assertFalse(admin_http.is_trusted_lan(peer_ip))

    def test_lan_warning_names_the_unauthenticated_interfaces(self):
        text = admin_http.lan_warning()
        self.assertIn('trusted lan', text.lower())
        self.assertIn('port-forward', text.lower())
        for marker in ('ONVIF', '/snapshot.jpg', '8554', '8555'):
            self.assertIn(marker, text)


# --------------------------------------------------------------------------- #
# Routing and mode transition
# --------------------------------------------------------------------------- #

class RoutingTests(AdminHttpTestBase):
    def test_known_routes_dispatch(self):
        self.assertEqual(self.app.handle(self.req('GET', '/api/status')).status, 200)
        self.assertEqual(self.app.handle(self.req('GET', '/admin')).status, 200)
        login = self.app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD}
        ))
        self.assertEqual(login.status, 200)

    def test_unknown_route_404(self):
        self.assertEqual(self.app.handle(self.req('GET', '/nope')).status, 404)

    def test_setup_routes_unavailable_in_admin_mode(self):
        self.assertEqual(self.app.handle(self.req('GET', '/setup')).status, 302)
        self.assertEqual(
            self.app.handle(self.req('POST', '/setup/step/1', body={})).status, 409
        )
        self.assertEqual(
            self.app.handle(self.req('POST', '/setup/finish', body={})).status, 409
        )

    def test_root_redirects_by_mode(self):
        admin_root = self.app.handle(self.req('GET', '/'))
        self.assertEqual(admin_root.status, 302)
        self.assertEqual(admin_root.headers['Location'], '/admin')

        setup_app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
        )
        setup_root = setup_app.handle(self.req('GET', '/'))
        self.assertEqual(setup_root.status, 302)
        self.assertEqual(setup_root.headers['Location'], '/setup')

    def test_setup_routes_available_while_unclaimed(self):
        setup_app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
        )
        self.assertEqual(setup_app.handle(self.req('GET', '/setup')).status, 200)

    def test_persisted_claimed_state_keeps_setup_open_for_finish(self):
        # ``persist`` writes a valid device + admin password and advances to
        # ``claimed`` (camera validated) while the runtime has not started: the
        # finish step must stay reachable to stop the AP and start the camera.
        path = self.root / 'provisioning.json'
        path.write_text(json.dumps({'state': 'claimed'}), encoding='utf-8')
        setup_app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
            provisioning_path=str(path),
        )
        self.assertEqual(setup_app.handle(self.req('GET', '/setup')).status, 200)
        self.assertNotEqual(
            setup_app.handle(self.req('POST', '/setup/finish', body={})).status, 409
        )

    def test_persisted_running_state_closes_setup_routes(self):
        path = self.root / 'provisioning.json'
        path.write_text(json.dumps({'state': 'running'}), encoding='utf-8')
        setup_app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='running'),
            provisioning_path=str(path),
        )
        redirect = setup_app.handle(self.req('GET', '/setup'))
        self.assertEqual(redirect.status, 302)
        self.assertEqual(redirect.headers['Location'], '/admin')
        self.assertEqual(
            setup_app.handle(self.req('POST', '/setup/step/1', body={})).status, 409
        )
        self.assertEqual(
            setup_app.handle(self.req('POST', '/setup/finish', body={})).status, 409
        )

    def test_missing_provisioning_file_falls_back_to_injected_state(self):
        # No state file exists, so the injected snapshot is authoritative. A
        # running snapshot must therefore close the portal (fail closed).
        setup_app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='running'),
            provisioning_path=str(self.root / 'absent-provisioning.json'),
        )
        redirect = setup_app.handle(self.req('GET', '/setup'))
        self.assertEqual(redirect.status, 302)
        self.assertEqual(redirect.headers['Location'], '/admin')
        self.assertEqual(
            setup_app.handle(self.req('POST', '/setup/step/1', body={})).status, 409
        )
        self.assertEqual(
            setup_app.handle(self.req('POST', '/setup/finish', body={})).status, 409
        )

    def test_empty_provisioning_path_falls_back_to_injected_state(self):
        setup_app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='running'),
            provisioning_path='',
        )
        self.assertEqual(setup_app.handle(self.req('GET', '/setup')).status, 302)


class MqttTestRouteTests(AdminHttpTestBase):
    """WP-R2 (AC-23 tail): the broker-test route and its secret hygiene."""

    URI = 'mqtts://broker.example:8883'
    MQTT_PASSWORD = 'mqtt-route-secret'

    def _setup_app(self, probe):
        return self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
            mqtt_probe=probe,
        )

    def _body(self):
        return {
            'uri': self.URI,
            'username': 'mqttuser',
            'password': self.MQTT_PASSWORD,
            'ca_file': '/etc/ssl/certs/ca.pem',
        }

    def test_unavailable_when_no_probe_injected(self):
        # Setup mode keeps the route public (the wizard calls it before saving).
        response = self._setup_app(None).handle(self.req(
            'POST', '/api/mqtt/test', body=self._body()))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['reason'], 'mqtt test unavailable')

    def test_setup_mode_public_success(self):
        seen = []

        def probe(config):
            seen.append(config)
            return True, 'connected'

        response = self._setup_app(probe).handle(self.req(
            'POST', '/api/mqtt/test', body=self._body()))
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(response.body)['ok'])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].uri, self.URI)
        self.assertEqual(seen[0].username, 'mqttuser')
        self.assertEqual(seen[0].password, self.MQTT_PASSWORD)

    def test_failure_reason_is_redacted(self):
        def probe(config):
            return False, f'bad credentials {self.MQTT_PASSWORD}'

        response = self._setup_app(probe).handle(self.req(
            'POST', '/api/mqtt/test', body=self._body()))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertFalse(payload['ok'])
        self.assertNotIn(self.MQTT_PASSWORD, payload['reason'])
        self.assertNotIn(self.MQTT_PASSWORD, response.body.decode('utf-8'))

    def test_raising_probe_is_isolated(self):
        def probe(config):
            raise RuntimeError(self.MQTT_PASSWORD)

        response = self._setup_app(probe).handle(self.req(
            'POST', '/api/mqtt/test', body=self._body()))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertFalse(payload['ok'])
        self.assertNotIn(self.MQTT_PASSWORD, response.body.decode('utf-8'))

    def test_missing_uri_is_rejected(self):
        response = self._setup_app(lambda config: (True, 'connected')).handle(
            self.req('POST', '/api/mqtt/test', body={'username': 'u'}))
        self.assertEqual(response.status, 400)

    def test_oversized_body_is_rejected(self):
        body = b'x' * (admin_http.MAX_MQTT_TEST_BODY_BYTES + 1)
        response = self._setup_app(lambda config: (True, 'connected')).handle(
            self.req('POST', '/api/mqtt/test', body=body))
        self.assertEqual(response.status, 413)

    def test_post_claim_requires_authentication(self):
        response = self.app.handle(self.req(
            'POST', '/api/mqtt/test', body=self._body()))
        self.assertEqual(response.status, 401)

    def test_post_claim_authenticated_succeeds(self):
        app = self._build_app(mqtt_probe=lambda config: (True, 'connected'))
        token = self.login(app=app)
        csrf = self.sessions.csrf_for(token)
        response = app.handle(self.req(
            'POST', '/api/mqtt/test', body=self._body(),
            headers=self.auth_headers(token, csrf=csrf)))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload['ok'])


# --------------------------------------------------------------------------- #
# Corrupt provisioning file / authoritative claim gate (fail closed)
# --------------------------------------------------------------------------- #

class SetupFailClosedTests(AdminHttpTestBase):
    """A corrupt state file or a factual claim must close the public wizard.

    Before this change a corrupt ``provisioning.json`` loaded as ``factory``,
    which is a pre-claim state, so disk corruption reopened the unauthenticated
    setup wizard and let it rewrite ``device.toml``/``secrets.toml`` -- a device
    takeover without the admin password. These tests pin the fail-closed fix and
    the second, facts-based :func:`provisioning.is_claimed` gate.
    """

    def _write_state(self, state):
        (self.root / 'provisioning.json').write_text(
            json.dumps({'state': state}), encoding='utf-8'
        )

    def _write_claimable_config(self):
        (self.root / 'device.toml').write_text(
            config_schema.dumps_device(config_schema.default_device()),
            encoding='utf-8',
        )
        (self.root / 'secrets.toml').write_text(
            config_schema.dumps_secrets({'admin': {'password_hash': ADMIN_HASH}}),
            encoding='utf-8',
        )

    def _setup_app(self, injected_state):
        return self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state=injected_state),
        )

    def _status(self, app):
        response = app.handle(self.req('GET', '/api/status'))
        self.assertEqual(response.status, 200)
        return json.loads(response.body.decode('utf-8'))

    def _assert_setup_closed(self, app):
        redirect = app.handle(self.req('GET', '/setup'))
        self.assertEqual(redirect.status, 302)
        self.assertEqual(redirect.headers['Location'], '/admin')
        self.assertEqual(
            app.handle(self.req('POST', '/setup/step/1', body={})).status, 409
        )
        self.assertEqual(
            app.handle(self.req('POST', '/setup/finish', body={})).status, 409
        )

    def test_corrupt_state_file_closes_setup_despite_claimed_injection(self):
        (self.root / 'provisioning.json').write_text('{not json', encoding='utf-8')
        app = self._setup_app('claimed')
        self._assert_setup_closed(app)
        payload = self._status(app)
        self.assertFalse(payload['setup_available'])
        self.assertNotEqual(payload['provisioning_state'], 'unclaimed')
        self.assertEqual(payload['provisioning_source'], 'persisted_corrupt')
        self.assertTrue(payload['provisioning_error'])

    def test_corrupt_state_file_closes_setup_despite_unclaimed_injection(self):
        # An empty (truncated/partial-write) file is corrupt, not a fresh device.
        (self.root / 'provisioning.json').write_text('', encoding='utf-8')
        app = self._setup_app('unclaimed')
        self._assert_setup_closed(app)
        payload = self._status(app)
        self.assertFalse(payload['setup_available'])
        self.assertNotEqual(payload['provisioning_state'], 'unclaimed')

    def test_invalid_state_value_closes_setup(self):
        self._write_state('bogus')
        app = self._setup_app('unclaimed')
        self._assert_setup_closed(app)
        payload = self._status(app)
        self.assertFalse(payload['setup_available'])
        self.assertEqual(payload['provisioning_error'],
                         'provisioning state value is invalid')

    def test_missing_state_file_unclaimed_serves_setup(self):
        app = self._setup_app('unclaimed')
        self.assertEqual(app.handle(self.req('GET', '/setup')).status, 200)
        payload = self._status(app)
        self.assertTrue(payload['setup_available'])
        self.assertEqual(payload['provisioning_state'], 'unclaimed')
        self.assertEqual(payload['provisioning_source'], 'injected')

    def test_missing_state_file_running_refuses_setup(self):
        app = self._setup_app('running')
        self._assert_setup_closed(app)
        payload = self._status(app)
        self.assertFalse(payload['setup_available'])
        self.assertEqual(payload['provisioning_state'], 'running')

    def test_claimable_in_finish_window_keeps_setup_open(self):
        # The bug this pins: persist writes a valid device + admin password, so
        # the device is claimable by facts while the state is still pre-runtime
        # (storage_ready when the camera is not validated). ``finish`` must stay
        # callable, otherwise the hotspot is never stopped and the camera target
        # never starts.
        self._write_claimable_config()
        self._write_state('storage_ready')
        app = self._setup_app('storage_ready')
        self.assertEqual(app.handle(self.req('GET', '/setup')).status, 200)
        self.assertNotEqual(
            app.handle(self.req('POST', '/setup/finish', body={})).status, 409
        )
        payload = self._status(app)
        self.assertTrue(payload['setup_available'])
        self.assertEqual(payload['provisioning_state'], 'claimed')

    def test_claimable_in_finish_window_keeps_setup_open_in_unclaimed_state(self):
        # ``persist`` leaves the state at ``unclaimed`` once the camera is
        # validated (and at ``storage_ready`` when it is not), while the device
        # is already claimable by facts. ``finish`` must stay callable in both
        # finish-window states, otherwise a failed/interrupted finish would
        # strand the device with neither the portal nor the camera runtime.
        self._write_claimable_config()
        self._write_state('unclaimed')
        app = self._setup_app('unclaimed')
        self.assertEqual(app.handle(self.req('GET', '/setup')).status, 200)
        self.assertNotEqual(
            app.handle(self.req('POST', '/setup/finish', body={})).status, 409
        )
        payload = self._status(app)
        self.assertTrue(payload['setup_available'])
        self.assertEqual(payload['provisioning_state'], 'claimed')
        self.assertEqual(payload['provisioning_source'], 'persisted')

    def test_status_consistent_after_persisted_flip(self):
        self._write_state('unclaimed')
        app = self._setup_app('unclaimed')
        before = self._status(app)
        self.assertTrue(before['setup_available'])
        self.assertEqual(before['provisioning_state'], 'unclaimed')

        self._write_state('running')
        after = self._status(app)
        self.assertFalse(after['setup_available'])
        self.assertEqual(after['provisioning_state'], 'running')
        # The invariant the shared view exists to guarantee: once the runtime
        # owns the device, setup is closed.
        self.assertFalse(
            after['setup_available'] and after['provisioning_state'] == 'running'
        )


# --------------------------------------------------------------------------- #
# Session policy
# --------------------------------------------------------------------------- #

class AuthPolicyTests(AdminHttpTestBase):
    def test_protected_route_without_session_401(self):
        self.assertEqual(self.app.handle(self.req('GET', '/api/config')).status, 401)

    def test_invalid_session_401(self):
        response = self.app.handle(self.req(
            'GET', '/api/config',
            headers={'Cookie': f'{SESSION_COOKIE}=forged-token'},
        ))
        self.assertEqual(response.status, 401)

    def test_expired_session_401(self):
        token = self.sessions.create(now=0.0, idle_ttl=1.0)
        response = self.app.handle(self.req(
            'GET', '/api/config', headers=self.auth_headers(token), now=10.0,
        ))
        self.assertEqual(response.status, 401)

    def test_valid_session_200(self):
        token = self.login()
        response = self.app.handle(self.req(
            'GET', '/api/config', headers=self.auth_headers(token),
        ))
        self.assertEqual(response.status, 200)


# --------------------------------------------------------------------------- #
# CSRF policy
# --------------------------------------------------------------------------- #

class CsrfTests(AdminHttpTestBase):
    def test_post_without_csrf_403(self):
        token = self.login()
        response = self.app.handle(self.req(
            'POST', '/api/logout', headers=self.auth_headers(token),
        ))
        self.assertEqual(response.status, 403)

    def test_post_with_bad_csrf_403(self):
        token = self.login()
        response = self.app.handle(self.req(
            'POST', '/api/logout', headers=self.auth_headers(token, csrf='not-the-token'),
        ))
        self.assertEqual(response.status, 403)

    def test_post_with_valid_csrf_proceeds(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        response = self.app.handle(self.req(
            'POST', '/api/logout', headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 200)
        self.assertIn('Max-Age=0', response.headers.get('Set-Cookie', ''))

    def test_csrf_token_is_bound_to_its_session(self):
        token_a = self.login(peer_ip='10.0.0.5')
        token_b = self.login(peer_ip='10.0.0.6')
        csrf_a = self.sessions.csrf_for(token_a)
        self.assertTrue(csrf_a)
        response = self.app.handle(self.req(
            'POST', '/api/logout',
            headers=self.auth_headers(token_b, csrf=csrf_a),
        ))
        self.assertEqual(response.status, 403)
        # Session B remains live: the CSRF failure did not revoke it.
        self.assertIsNotNone(self.sessions.csrf_for(token_b))


# --------------------------------------------------------------------------- #
# Session cookie hardening
# --------------------------------------------------------------------------- #

class CookieTests(AdminHttpTestBase):
    def test_admin_mode_cookie_is_secure_httponly_samesite_lax(self):
        response = self.app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD},
        ))
        self.assertEqual(response.status, 200)
        cookie = response.headers.get('Set-Cookie', '')
        self.assertIn('Secure', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)

    def test_setup_mode_cookie_omits_secure_but_keeps_httponly(self):
        app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
        )
        response = app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD},
        ))
        self.assertEqual(response.status, 200)
        cookie = response.headers.get('Set-Cookie', '')
        self.assertNotIn('Secure', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)


# --------------------------------------------------------------------------- #
# Setup-mode login
# --------------------------------------------------------------------------- #

class SetupLoginTests(AdminHttpTestBase):
    def test_login_works_in_setup_mode_with_admin_hash(self):
        app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
        )
        response = app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD},
        ))
        self.assertEqual(response.status, 200)

    def test_login_fails_closed_without_admin_hash(self):
        app = self._build_app(
            mode='setup',
            admin_hash=None,
            secrets_path=str(self.root / 'missing-secrets.toml'),
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
        )
        response = app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD},
        ))
        self.assertEqual(response.status, 401)


# --------------------------------------------------------------------------- #
# Login rate limiting (keyed on the socket peer, never a header)
# --------------------------------------------------------------------------- #

class RateLimitTests(AdminHttpTestBase):
    def test_failures_lock_the_peer_ip(self):
        for _ in range(3):
            response = self.app.handle(self.req(
                'POST', '/api/login', body={'password': 'wrong'},
                peer_ip='10.0.0.5',
            ))
            self.assertEqual(response.status, 401)
        locked = self.app.handle(self.req(
            'POST', '/api/login', body={'password': 'wrong'}, peer_ip='10.0.0.5',
        ))
        self.assertEqual(locked.status, 429)
        self.assertIn('Retry-After', locked.headers)
        self.assertGreaterEqual(int(locked.headers['Retry-After']), 1)

    def test_a_different_peer_is_unaffected(self):
        for _ in range(3):
            self.app.handle(self.req(
                'POST', '/api/login', body={'password': 'wrong'}, peer_ip='10.0.0.5',
            ))
        response = self.app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD}, peer_ip='10.0.0.6',
        ))
        self.assertEqual(response.status, 200)

    def test_forwarded_for_header_does_not_change_the_key(self):
        for index in range(3):
            self.app.handle(self.req(
                'POST', '/api/login', body={'password': 'wrong'},
                headers={'X-Forwarded-For': f'1.2.3.{index}'}, peer_ip='10.0.0.5',
            ))
        same_peer = self.app.handle(self.req(
            'POST', '/api/login', body={'password': 'wrong'},
            headers={'X-Forwarded-For': '9.9.9.9'}, peer_ip='10.0.0.5',
        ))
        self.assertEqual(same_peer.status, 429)

        other_peer = self.app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD},
            headers={'X-Forwarded-For': '1.2.3.0'}, peer_ip='10.0.0.6',
        ))
        self.assertEqual(other_peer.status, 200)


# --------------------------------------------------------------------------- #
# Re-authentication policy
# --------------------------------------------------------------------------- #

class ReauthTests(AdminHttpTestBase):
    def test_reauth_route_without_password_403(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        response = self.app.handle(self.req(
            'POST', '/api/ssh', body={'enabled': True},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 403)

    def test_reauth_with_correct_password_proceeds(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        response = self.app.handle(self.req(
            'POST', '/api/ssh', body={'enabled': True, 'password': ADMIN_PASSWORD},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers.get('Content-Type', '').startswith('application/json'))
        self.assertTrue(self.ssh_runner.calls)

    def test_reauth_window_expires(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        primed = self.app.handle(self.req(
            'POST', '/api/ssh', body={'enabled': True, 'password': ADMIN_PASSWORD},
            headers=self.auth_headers(token, csrf=csrf), now=NOW,
        ))
        self.assertEqual(primed.status, 200)

        later = NOW + admin_http.REAUTH_WINDOW_SECONDS + 1.0
        expired = self.app.handle(self.req(
            'POST', '/api/ssh', body={'enabled': False},
            headers=self.auth_headers(token, csrf=csrf), now=later,
        ))
        self.assertEqual(expired.status, 403)

    def test_reauth_is_rate_limited(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        for _ in range(3):
            response = self.app.handle(self.req(
                'POST', '/api/reauth', body={'password': 'wrong'},
                headers=self.auth_headers(token, csrf=csrf),
            ))
            self.assertEqual(response.status, 403)
        locked = self.app.handle(self.req(
            'POST', '/api/reauth', body={'password': 'wrong'},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(locked.status, 429)
        self.assertIn('Retry-After', locked.headers)

    def test_forged_session_cannot_reach_a_reauth_handler(self):
        response = self.app.handle(self.req(
            'POST', '/api/reauth', body={'password': ADMIN_PASSWORD},
            headers={'Cookie': f'{SESSION_COOKIE}=forged-token'},
        ))
        self.assertEqual(response.status, 401)

    def test_recovery_route_requires_reauth(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        response = self.app.handle(self.req(
            'POST', '/api/recovery/enter-setup', body={},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 403)

    def test_recovery_with_reauth_writes_the_sentinel(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        response = self.app.handle(self.req(
            'POST', '/api/recovery/enter-setup',
            body={'password': ADMIN_PASSWORD, 'reason': 'operator'},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 200)
        self.assertTrue((self.root / 'pibuddycam-recovery').exists())


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

class RedactionTests(AdminHttpTestBase):
    def test_login_response_contains_no_secret(self):
        response = self.app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD},
        ))
        body = response.body.decode('utf-8')
        self.assertNotIn(ADMIN_PASSWORD, body)
        self.assertNotIn(ADMIN_HASH, body)
        token = _parse_cookie(response)
        self.assertTrue(token)
        self.assertNotIn(token, body)

    def test_config_response_redacts_secrets(self):
        (self.root / 'device.toml').write_text(
            config_schema.dumps_device(config_schema.default_device())
        )
        (self.root / 'secrets.toml').write_text(config_schema.dumps_secrets({
            'prusa': {'token': 'tok-abc-123'},
            'wifi': {'psk': 'sup3rsecret'},
            'admin': {'password_hash': ADMIN_HASH},
        }))
        token = self.login()
        response = self.app.handle(self.req(
            'GET', '/api/config', headers=self.auth_headers(token),
        ))
        self.assertEqual(response.status, 200)
        body = response.body.decode('utf-8')
        for secret in ('tok-abc-123', 'sup3rsecret', ADMIN_HASH):
            self.assertNotIn(secret, body)
        self.assertIn(admin_auth.REDACTED, body)

    def test_logs_contain_no_secret(self):
        with self.assertLogs('pibuddycam.admin_http', level='DEBUG') as captured:
            self.app.handle(self.req(
                'GET', '/api/status',
                headers={'Authorization': 'Bearer topsecret-credential'},
            ))
        joined = '\n'.join(captured.output)
        self.assertNotIn('topsecret-credential', joined)

    def test_csrf_header_absent_from_logs(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        self.assertTrue(csrf)
        with self.assertLogs('pibuddycam.admin_http', level='DEBUG') as captured:
            response = self.app.handle(self.req(
                'POST', '/api/logout',
                headers=self.auth_headers(token, csrf=csrf),
            ))
        self.assertEqual(response.status, 200)
        joined = '\n'.join(captured.output)
        self.assertNotIn(csrf, joined)
        self.assertNotIn('csrf-token', joined.lower())


# --------------------------------------------------------------------------- #
# Status / trusted-LAN labelling
# --------------------------------------------------------------------------- #

class StatusTests(AdminHttpTestBase):
    def test_status_includes_the_trusted_lan_warning(self):
        app = self._build_app(hotspot=self.hotspot)
        response = app.handle(self.req('GET', '/api/status'))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body.decode('utf-8'))
        notice = payload['trusted_lan']['notice']
        self.assertIn('trusted lan', notice.lower())
        self.assertIn('port-forward', notice.lower())
        interfaces = payload['trusted_lan']['interfaces']
        ports = [item.get('port') for item in interfaces]
        self.assertIn(8554, ports)
        self.assertIn(8555, ports)
        names = ' '.join(item.get('name', '') for item in interfaces)
        self.assertIn('ONVIF', names)
        paths = ' '.join(item.get('path', '') for item in interfaces)
        self.assertIn('/snapshot.jpg', paths)

    def test_status_reports_injected_hotspot(self):
        app = self._build_app(hotspot=self.hotspot)
        payload = json.loads(app.handle(self.req('GET', '/api/status')).body.decode('utf-8'))
        self.assertTrue(payload['hotspot']['active'])
        self.assertEqual(payload['hotspot']['address'], '192.168.4.1')

    def test_camera_probe_only_runs_in_setup_mode(self):
        # Post-runtime rpicam-source owns libcamera (single consumer), so probing
        # would fail and misreport a working camera as unavailable. Setup mode
        # probes; admin mode must not.
        calls = []

        class _Probe:
            ok = True
            reason = ''
            sensors = 1

        def probe():
            calls.append(True)
            return _Probe()

        admin = self._build_app(probe=probe)
        admin_payload = json.loads(
            admin.handle(self.req('GET', '/api/status')).body.decode('utf-8'))
        self.assertNotIn('camera', admin_payload)
        self.assertEqual(calls, [])

        setup = self._build_app(mode='setup', probe=probe)
        setup_payload = json.loads(
            setup.handle(self.req('GET', '/api/status')).body.decode('utf-8'))
        self.assertIn('camera', setup_payload)
        self.assertTrue(setup_payload['camera']['ok'])
        self.assertEqual(calls, [True])


# --------------------------------------------------------------------------- #
# Setup wizard
# --------------------------------------------------------------------------- #

class WizardTests(AdminHttpTestBase):
    def _setup_app(self):
        self.wizard = setup_wizard.WizardSession(
            device_id='AA:BB:CC:DD:EE:FF',
            device_path=str(self.root / 'device.toml'),
            secrets_path=str(self.root / 'secrets.toml'),
            provisioning_path=str(self.root / 'provisioning.json'),
            storage_ready=True,
            probe_result=SimpleNamespace(ok=True, reason='', sensors=1),
        )
        return self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
            wizard_factory=lambda: self.wizard,
        )

    def test_invalid_step_400_and_no_advance(self):
        app = self._setup_app()
        response = app.handle(self.req(
            'POST', '/setup/step/3', body={'ssid': ''},
        ))
        self.assertEqual(response.status, 400)
        self.assertEqual(self.wizard.step, setup_wizard.STEP_ORDER[0])
        self.assertNotIn('wifi', self.wizard.completed)

    def test_valid_step_advances(self):
        app = self._setup_app()
        response = app.handle(self.req(
            'POST', '/setup/step/1', body={'storage_ready': True},
        ))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.wizard.step, 'imager_prefill')
        self.assertIn('status', self.wizard.completed)

    def test_unknown_step_number_400(self):
        app = self._setup_app()
        self.assertEqual(
            app.handle(self.req('POST', '/setup/step/99', body={})).status, 400
        )
        self.assertEqual(
            app.handle(self.req('POST', '/setup/step/abc', body={})).status, 400
        )

    def test_setup_post_must_be_json(self):
        app = self._setup_app()
        for path in ('/setup/step/1', '/setup/finish', '/setup/wifi/scan'):
            response = app.handle(self.req(
                'POST', path, body='storage_ready=1',
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
            ))
            self.assertEqual(response.status, 415, path)
        # A JSON-looking body under a CORS-simple content type is refused too.
        response = app.handle(self.req(
            'POST', '/setup/step/1', body='{}',
            headers={'Content-Type': 'text/plain; charset=application/json'},
        ))
        self.assertEqual(response.status, 415)
        self.assertNotIn('status', self.wizard.completed)

    def test_setup_page_serves_the_wizard_shell(self):
        response = self._setup_app().handle(self.req('GET', '/setup'))
        self.assertEqual(response.status, 200)
        html = response.body.decode('utf-8')
        self.assertIn('/assets/setup.js?v=', html)
        self.assertNotIn('__ASSET_VERSION__', html)
        self.assertNotIn('Submit this step through the setup API', html)
        self.assertEqual(response.headers['Content-Security-Policy'], admin_http.HTML_CSP)

    def test_setup_page_falls_back_when_the_shell_is_missing(self):
        app = self._setup_app()
        app._web_dir = str(self.root / 'no-web')
        response = app.handle(self.req('GET', '/setup'))
        self.assertEqual(response.status, 200)
        self.assertIn(b'Storage and camera status', response.body)

    def test_setup_state_is_redacted_and_reports_saved_flags(self):
        app = self._setup_app()
        token = 'prusa-token-route-secret'
        psk = 'wifi-psk-route-secret'
        for number, body in (
            (1, {}), (2, {}), (3, {'ssid': 'Home', 'psk': psk}),
            (4, {'source': 'manual', 'token': token}),
            (6, {'password': ADMIN_PASSWORD, 'confirm': ADMIN_PASSWORD}),
        ):
            response = app.handle(self.req('POST', f'/setup/step/{number}', body=body))
            self.assertEqual(response.status, 200, (number, response.body))
        response = app.handle(self.req('GET', '/setup/state'))
        self.assertEqual(response.status, 200)
        raw = response.body.decode('utf-8')
        for secret in (token, psk, ADMIN_PASSWORD, self.wizard.admin_hash):
            self.assertNotIn(secret, raw)
        payload = json.loads(raw)
        # The pointer follows the last submitted step; the page resumes from
        # ``completed`` instead, so a skipped optional step is never lost.
        self.assertEqual(payload['step'], 'mqtt')
        self.assertEqual(payload['step_number'], 7)
        self.assertEqual(payload['done_steps'], [1, 2, 3, 4, 6])
        self.assertEqual(len(payload['steps']), len(setup_wizard.STEP_ORDER))
        self.assertEqual(payload['summary']['wifi']['ssid'], 'Home')
        self.assertEqual(payload['saved'], {
            'wifi_secured': True, 'prusa_ready': True,
            'admin_ready': True, 'fingerprint_pinned': False,
        })
        self.assertEqual(payload['setup_ssid'], 'PiBuddyCam-Setup-ddeeff')
        self.assertEqual(payload['admin_url'], 'https://pibuddycam-ddeeff.local')
        self.assertEqual(response.headers['Cache-Control'], admin_http.NO_STORE_CACHE_CONTROL)

    def test_setup_state_closed_after_claim(self):
        path = self.root / 'provisioning.json'
        path.write_text(json.dumps({'state': 'running'}), encoding='utf-8')
        app = self._setup_app()
        response = app.handle(self.req('GET', '/setup/state'))
        self.assertEqual(response.status, 302)
        self.assertEqual(
            app.handle(self.req('POST', '/setup/wifi/scan', body={})).status, 409)

    def test_setup_state_step_numbers_survive_secret_scrubbing(self):
        # A short MQTT username that is a substring of a step name must not
        # corrupt the resume information.
        app = self._setup_app()
        for number, body in (
            (1, {}), (2, {}), (3, {'ssid': 'Home', 'psk': 'wifi-psk-route'}),
            (4, {'source': 'manual', 'token': 'prusa-token-route'}),
            (5, {}), (6, {'password': ADMIN_PASSWORD, 'confirm': ADMIN_PASSWORD}),
            (7, {'enabled': True, 'uri': 'mqtt://broker.example:1883',
                 'username': 'admin', 'password': 'pass'}),
        ):
            response = app.handle(self.req('POST', f'/setup/step/{number}', body=body))
            self.assertEqual(response.status, 200, (number, response.body))
        payload = json.loads(app.handle(self.req('GET', '/setup/state')).body)
        self.assertEqual(payload['done_steps'], [1, 2, 3, 4, 5, 6, 7])

    def test_setup_scan_returns_bounded_networks(self):
        app = self._setup_app()
        self.wizard.wifi_scan = lambda: [
            {'ssid': 'Home', 'signal': 80, 'secured': True},
            {'ssid': '', 'signal': 10, 'secured': False},
            'garbage',
        ]
        response = app.handle(self.req('POST', '/setup/wifi/scan', body={}))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['networks'], [{'ssid': 'Home', 'signal': 80, 'secured': True}])
        state = json.loads(app.handle(self.req('GET', '/setup/state')).body)
        self.assertEqual(state['networks'], payload['networks'])

    def test_setup_scan_failure_and_unavailable(self):
        app = self._setup_app()
        payload = json.loads(app.handle(self.req('POST', '/setup/wifi/scan', body={})).body)
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['reason'], 'wifi scanning is not available')

        def broken():
            raise RuntimeError('wifi scan unavailable')

        self.wizard.wifi_scan = broken
        payload = json.loads(app.handle(self.req('POST', '/setup/wifi/scan', body={})).body)
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['reason'], 'wifi scan failed')
        self.assertEqual(payload['networks'], [])

    def test_activate_station_is_passed_to_the_wizard(self):
        def activate(ssid, psk):
            return True

        app = self._build_app(activate_station=activate)
        self.assertIs(app._get_wizard().activate_station, activate)

    def test_activate_station_defaults_to_none(self):
        app = self._build_app()
        self.assertIsNone(app._get_wizard().activate_station)


# --------------------------------------------------------------------------- #
# Expert configuration
# --------------------------------------------------------------------------- #

class ExpertConfigTests(AdminHttpTestBase):
    def test_invalid_put_400_and_live_files_unchanged(self):
        device_path = self.root / 'device.toml'
        secrets_path = self.root / 'secrets.toml'
        device_path.write_bytes(b'original-device-bytes')
        secrets_path.write_bytes(b'original-secrets-bytes')

        token = self.login()
        csrf = self.sessions.csrf_for(token)
        response = self.app.handle(self.req(
            'PUT', '/api/config',
            body={'device': '[[[unterminated', 'secrets': 'x = 1'},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 400)
        self.assertEqual(device_path.read_bytes(), b'original-device-bytes')
        self.assertEqual(secrets_path.read_bytes(), b'original-secrets-bytes')


# --------------------------------------------------------------------------- #
# Factory reset two-step flow
# --------------------------------------------------------------------------- #

class FactoryResetTests(AdminHttpTestBase):
    def _reset_app(self):
        controller = factory_reset.FactoryReset(
            data_root=str(self.root), durable_root=str(self.root),
        )
        os.makedirs(controller.config_dir, exist_ok=True)
        marker = Path(controller.config_dir) / 'device.toml'
        marker.write_text('keep-me')
        return self._build_app(factory_reset=controller), marker

    def _prime(self, app, token, csrf):
        """Prime the fresh re-auth window (the reset routes are window-only)."""
        response = app.handle(self.req(
            'POST', '/api/reauth', body={'password': ADMIN_PASSWORD},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 200, response.body)

    def test_execute_without_reauth_403(self):
        app, marker = self._reset_app()
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        response = app.handle(self.req(
            'POST', '/api/reset/execute', body={},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 403)
        self.assertTrue(marker.exists())

    def test_inline_password_is_not_read_by_a_window_only_reset_route(self):
        # A reset body's ``password`` must never be interpreted as the admin
        # password (or charged to the login limiter): the route is window-only.
        app, marker = self._reset_app()
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        response = app.handle(self.req(
            'POST', '/api/reset/begin',
            body={'password': ADMIN_PASSWORD, 'reason': 'operator'},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 403)
        # The limiter was not charged: a correct reauth still succeeds.
        self._prime(app, token, csrf)
        allowed = app.handle(self.req(
            'POST', '/api/reset/begin', body={'reason': 'operator'},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(allowed.status, 200)

    def test_unknown_reset_field_is_rejected_after_the_window(self):
        app, marker = self._reset_app()
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        self._prime(app, token, csrf)
        response = app.handle(self.req(
            'POST', '/api/reset/begin',
            body={'password': 'not-an-admin-password', 'reason': 'operator'},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 400)
        self.assertTrue(marker.exists())

    def test_execute_before_begin_or_confirm_deletes_nothing(self):
        app, marker = self._reset_app()
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        self._prime(app, token, csrf)

        before_begin = app.handle(self.req(
            'POST', '/api/reset/execute', body={},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(before_begin.status, 409)
        self.assertTrue(marker.exists())

        app.handle(self.req(
            'POST', '/api/reset/begin', body={},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        before_confirm = app.handle(self.req(
            'POST', '/api/reset/execute', body={},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(before_confirm.status, 409)
        self.assertTrue(marker.exists())

    def test_confirm_with_wrong_explicit_token_409(self):
        app, marker = self._reset_app()
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        self._prime(app, token, csrf)
        begin = app.handle(self.req(
            'POST', '/api/reset/begin', body={},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(begin.status, 200)

        rejected = app.handle(self.req(
            'POST', '/api/reset/confirm',
            body={'token': 'not-the-server-token'},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(rejected.status, 409)

        # The wrong token must not have armed the reset.
        after = app.handle(self.req(
            'POST', '/api/reset/execute', body={},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(after.status, 409)
        self.assertTrue(marker.exists())

    def test_include_timelapse_must_be_a_boolean(self):
        app, marker = self._reset_app()
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        self._prime(app, token, csrf)
        app.handle(self.req(
            'POST', '/api/reset/begin', body={},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        app.handle(self.req(
            'POST', '/api/reset/confirm', body={},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        response = app.handle(self.req(
            'POST', '/api/reset/execute', body={'include_timelapse': 'yes'},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 400)
        self.assertTrue(marker.exists())


# --------------------------------------------------------------------------- #
# Local live monitor (WP-UI5; AC-10/AC-11/AC-17)
# --------------------------------------------------------------------------- #

def _live_jpeg():
    return b'\xff\xd8' + b'camera-frame' * 20 + b'\xff\xd9'


class _FakeMonitor:
    """Deterministic stand-in for :class:`live_monitor.LiveMonitor`."""

    def __init__(self, frame=None, state='live', touch_ok=True, metrics=None):
        self.frame = frame
        self.state = state
        self.touch_ok = touch_ok
        self.metrics_value = metrics or {}
        self.touched = []

    def touch(self, viewer_id):
        self.touched.append(viewer_id)
        return self.touch_ok

    def latest(self):
        return {'state': self.state, 'jpeg': self.frame, 'age_seconds': 1.0}

    def metrics(self):
        return dict(self.metrics_value)


class LiveMonitorRouteTests(AdminHttpTestBase):
    def test_routes_require_authentication(self):
        for path in ('/api/live/frame', '/api/live/status'):
            with self.subTest(path=path):
                self.assertEqual(self.app.handle(self.req('GET', path)).status, 401)

    def test_frame_requires_a_live_session(self):
        token = self.sessions.create(now=0.0, idle_ttl=1.0)
        response = self.app.handle(self.req(
            'GET', '/api/live/frame',
            headers=self.auth_headers(token), now=10.0,
        ))
        self.assertEqual(response.status, 401)

    def test_frame_is_served_with_no_store_and_state_headers(self):
        monitor = _FakeMonitor(frame=_live_jpeg(), state='live')
        app = self._build_app(live_monitor=monitor)
        token = self.login(app)
        response = app.handle(self.req(
            'GET', '/api/live/frame', headers=self.auth_headers(token)))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, _live_jpeg())
        self.assertEqual(response.headers.get('Content-Type'), 'image/jpeg')
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))
        self.assertEqual(response.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(response.headers.get('X-Live-State'), 'live')
        self.assertEqual(response.headers.get('X-Live-Age'), '1.0')
        # A repeated poll by the same session reuses the same stable viewer ID,
        # so it refreshes the lease instead of consuming another cap slot.
        second = app.handle(self.req(
            'GET', '/api/live/frame', headers=self.auth_headers(token)))
        self.assertEqual(second.status, 200)
        self.assertEqual(monitor.touched, [token, token])

    def test_missing_monitor_reports_unavailable(self):
        response = self.app.handle(self.req(
            'GET', '/api/live/frame', headers=self.auth_headers(self.login())))
        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers.get('X-Live-State'), 'unavailable')
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))
        self.assertEqual(response.headers.get('X-Content-Type-Options'), 'nosniff')

    def test_stale_frame_is_never_served(self):
        monitor = _FakeMonitor(frame=None, state='stale')
        app = self._build_app(live_monitor=monitor)
        token = self.login(app)
        response = app.handle(self.req(
            'GET', '/api/live/frame', headers=self.auth_headers(token)))
        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers.get('X-Live-State'), 'stale')

    def test_partial_frame_from_a_monitor_is_never_served(self):
        monitor = _FakeMonitor(frame=b'\xff\xd8' + b'x' * 10, state='live')
        app = self._build_app(live_monitor=monitor)
        token = self.login(app)
        response = app.handle(self.req(
            'GET', '/api/live/frame', headers=self.auth_headers(token)))
        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers.get('X-Live-State'), 'live')
        self.assertNotIn(b'\xff\xd8', response.body)

    def test_viewer_cap_returns_429(self):
        monitor = _FakeMonitor(frame=_live_jpeg(), touch_ok=False)
        app = self._build_app(live_monitor=monitor)
        token = self.login(app)
        response = app.handle(self.req(
            'GET', '/api/live/frame', headers=self.auth_headers(token)))
        self.assertEqual(response.status, 429)
        self.assertEqual(response.headers.get('Retry-After'), '5')
        self.assertEqual(response.headers.get('X-Live-State'), 'busy')

    def test_status_reports_bounded_metrics(self):
        metrics = {
            'source': 'stream_mux:8888',
            'interval_seconds': 1.0,
            'producer': {'running': True, 'frames_produced': 3},
            'viewers': {'active': 1, 'cap': 4},
            'frame': {'state': 'live'},
        }
        monitor = _FakeMonitor(metrics=metrics)
        app = self._build_app(live_monitor=monitor)
        token = self.login(app)
        response = app.handle(self.req(
            'GET', '/api/live/status', headers=self.auth_headers(token)))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload['ok'])
        self.assertTrue(payload['available'])
        self.assertEqual(payload['source'], 'stream_mux:8888')
        self.assertEqual(payload['producer']['frames_produced'], 3)

    def test_status_without_monitor_is_available_false(self):
        response = self.app.handle(self.req(
            'GET', '/api/live/status', headers=self.auth_headers(self.login())))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload['ok'])
        self.assertFalse(payload['available'])

    def test_close_stops_the_shared_producer(self):
        monitor = live_monitor.LiveMonitor(
            producer=_live_jpeg, interval=0.05, idle_grace=5.0, viewer_ttl=5.0)
        app = self._build_app(live_monitor=monitor)
        token = self.login(app)
        # The first frame may not be decoded yet, but the authenticated request
        # must have leased a viewer and started the one shared producer.
        response = app.handle(self.req(
            'GET', '/api/live/frame', headers=self.auth_headers(token)))
        self.assertIn(response.status, (200, 503))
        self.assertTrue(monitor.running)
        app.close()
        self.assertFalse(monitor.running)
        app.close()  # idempotent shutdown

    def test_status_redacts_planted_secret_canary(self):
        canary = 'CANARY-live-secret-3f9a'
        (self.root / 'secrets.toml').write_text(config_schema.dumps_secrets({
            'prusa': {'token': canary},
            'admin': {'password_hash': ADMIN_HASH},
        }))
        monitor = _FakeMonitor(metrics={'leaked': canary, 'source': 'stream_mux:8888'})
        app = self._build_app(live_monitor=monitor)
        token = self.login(app)
        response = app.handle(self.req(
            'GET', '/api/live/status', headers=self.auth_headers(token)))
        self.assertEqual(response.status, 200)
        self.assertNotIn(canary.encode(), response.body)
        self.assertNotIn(canary, response.headers.get('Set-Cookie', ''))


# --------------------------------------------------------------------------- #
# Timelapse media library (WP-UI6; AC-12/AC-13/AC-17)
# --------------------------------------------------------------------------- #

def _media_jpeg(width=4, height=4):
    import struct as _struct
    sof = (
        b'\xff\xc0' + _struct.pack('>H', 17) + b'\x08'
        + _struct.pack('>HH', height, width)
        + b'\x03' + b'\x01\x11\x00' * 3
    )
    return b'\xff\xd8' + sof + b'\xff\xd9'


class _FakeBuildManager:
    """Deterministic stand-in for :class:`media_build.BuildManager`."""

    def __init__(self, result=None):
        self.result = result if result is not None else {
            'ok': True,
            'job': {
                'id': '0' * 32, 'state': 'pending', 'frames_total': 3,
                'frames_written': 0, 'reason': '',
                'started': '2026-01-01T00:00:00Z', 'finished': None,
            },
        }
        self.starts = []
        self.stopped = False

    def start(self, fps=10, width=None, height=None):
        self.starts.append(fps)
        return self.result

    def status(self, job_id):
        job = self.result.get('job') if self.result.get('ok') else None
        if isinstance(job, dict) and job.get('id') == job_id:
            return job
        return None

    def stop(self):
        self.stopped = True


class MediaLibraryRouteTests(AdminHttpTestBase):
    FRAME = 'timelapse_00-00-00-000.jpg'
    FRAME2 = 'timelapse_00-00-01-000.jpg'
    VIDEO = 'timelapse_00-00-00-000.avi'

    def setUp(self):
        super().setUp()
        self.media = self.root / 'media'
        self.media.mkdir()
        (self.media / self.FRAME).write_bytes(_media_jpeg())
        (self.media / self.FRAME2).write_bytes(_media_jpeg(8, 8))
        (self.media / self.VIDEO).write_bytes(b'RIFF' + b'v' * 120 + b'AVI ')
        (self.media / '.timelapse_videos.csv').write_text(
            f'{self.VIDEO}:D\n', encoding='utf-8')
        self.build = _FakeBuildManager()
        self.app = self._build_app(
            media_dir=str(self.media), build_manager=self.build)

    def _req(self, method, path, query=None, body=None, headers=None):
        request = self.req(method, path, body=body, headers=headers)
        request.query = query or {}
        return request

    def _auth(self, token, csrf=None, extra=None):
        return self.auth_headers(token, csrf=csrf, extra=extra)

    def test_media_routes_require_authentication(self):
        for method, path in (
            ('GET', '/api/media/timelapses'),
            ('GET', f'/api/media/timelapses/{self.VIDEO}'),
            ('GET', '/api/media/frames'),
            ('GET', f'/api/media/frames/{self.FRAME}'),
            ('POST', '/api/media/timelapses/build'),
            ('GET', '/api/media/jobs/' + '0' * 32),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.app.handle(self._req(method, path)).status, 401)

    def test_video_list_paginates_and_maps_status(self):
        token = self.login(self.app)
        response = self.app.handle(self._req(
            'GET', '/api/media/timelapses', query={'page': '1', 'page_size': '1'},
            headers=self._auth(token)))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['kind'], 'videos')
        self.assertEqual(payload['page'], 1)
        self.assertEqual(payload['page_size'], 1)
        self.assertEqual(payload['total'], 1)
        item = payload['items'][0]
        self.assertEqual(item['name'], self.VIDEO)
        self.assertEqual(item['status'], 'D')
        self.assertEqual(item['status_label'], 'completed')
        self.assertEqual(item['download_url'], f'/api/media/timelapses/{self.VIDEO}')
        self.assertNotIn('.timelapse_videos.csv', response.body.decode())
        self.assertNotIn(str(self.media), response.body.decode())

    def test_status_filter_accepts_label_and_char(self):
        token = self.login(self.app)
        for status in ('completed', 'D'):
            response = self.app.handle(self._req(
                'GET', '/api/media/timelapses', query={'status': status},
                headers=self._auth(token)))
            payload = json.loads(response.body)
            self.assertEqual([i['name'] for i in payload['items']], [self.VIDEO])
        response = self.app.handle(self._req(
            'GET', '/api/media/timelapses', query={'status': 'error'},
            headers=self._auth(token)))
        self.assertEqual(json.loads(response.body)['items'], [])

    def test_invalid_page_bounds_and_status(self):
        token = self.login(self.app)
        for query in ({'page': '0'}, {'page': 'x'}, {'page_size': '0'},
                      {'page_size': '101'}, {'page_size': 'x'}, {'status': 'weird'}):
            with self.subTest(query=query):
                response = self.app.handle(self._req(
                    'GET', '/api/media/timelapses', query=query,
                    headers=self._auth(token)))
                self.assertEqual(response.status, 400)
                self.assertFalse(json.loads(response.body)['ok'])

    def test_frame_list_bounded(self):
        token = self.login(self.app)
        response = self.app.handle(self._req(
            'GET', '/api/media/frames', query={'page_size': '1'},
            headers=self._auth(token)))
        payload = json.loads(response.body)
        self.assertEqual(payload['kind'], 'frames')
        self.assertEqual(payload['total'], 2)
        self.assertEqual(len(payload['items']), 1)
        self.assertEqual(payload['items'][0]['preview_url'],
                         f'/api/media/frames/{self.FRAME}')
        # Frames have no firmware index status; the API must not invent one.
        self.assertNotIn('status', payload['items'][0])
        self.assertNotIn('status_label', payload['items'][0])

    def test_video_download_whole(self):
        token = self.login(self.app)
        response = self.app.handle(self._req(
            'GET', f'/api/media/timelapses/{self.VIDEO}',
            headers=self._auth(token)))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Content-Type'], 'video/x-msvideo')
        self.assertEqual(
            response.headers['Content-Disposition'],
            f'attachment; filename="{self.VIDEO}"')
        self.assertEqual(response.headers['Accept-Ranges'], 'bytes')
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(response.file_length, os.path.getsize(self.media / self.VIDEO))
        try:
            self.assertEqual(response.file.read(), (self.media / self.VIDEO).read_bytes())
        finally:
            response.file.close()

    def test_video_range_is_partial_and_bounded(self):
        token = self.login(self.app)
        response = self.app.handle(self._req(
            'GET', f'/api/media/timelapses/{self.VIDEO}',
            headers=self._auth(token, extra={'Range': 'bytes=0-9'})))
        self.assertEqual(response.status, 206)
        self.assertEqual(response.headers['Content-Range'], 'bytes 0-9/128')
        self.assertEqual(response.headers['Content-Length'], '10')
        self.assertEqual(response.file_length, 10)
        try:
            self.assertEqual(
                response.file.read(response.file_length),
                (self.media / self.VIDEO).read_bytes()[:10])
        finally:
            response.file.close()

    def test_range_malformed_and_unsatisfiable_are_416(self):
        token = self.login(self.app)
        for value in ('bytes=9999-', 'bytes=1-2,3-4', 'bytes=abc', 'items=0-1'):
            with self.subTest(value=value):
                response = self.app.handle(self._req(
                    'GET', f'/api/media/timelapses/{self.VIDEO}',
                    headers=self._auth(token, extra={'Range': value})))
                self.assertEqual(response.status, 416)
                self.assertEqual(response.headers['Content-Range'], 'bytes */128')
                self.assertEqual(response.headers['Accept-Ranges'], 'bytes')

    def test_frame_download_is_bounded_and_validated(self):
        token = self.login(self.app)
        response = self.app.handle(self._req(
            'GET', f'/api/media/frames/{self.FRAME}',
            headers=self._auth(token)))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Content-Type'], 'image/jpeg')
        self.assertIn('inline;', response.headers['Content-Disposition'])
        self.assertEqual(response.file_length, os.path.getsize(self.media / self.FRAME))
        try:
            self.assertTrue(response.file.read().startswith(b'\xff\xd8\xff'))
        finally:
            response.file.close()

    def test_frame_without_jpeg_magic_is_404(self):
        (self.media / self.FRAME2).write_bytes(b'not a jpeg')
        token = self.login(self.app)
        response = self.app.handle(self._req(
            'GET', f'/api/media/frames/{self.FRAME2}',
            headers=self._auth(token)))
        self.assertEqual(response.status, 404)

    def test_planted_paths_symlinks_and_case_are_404(self):
        (self.media / 'a.AVI').write_bytes(b'RIFF')
        os.symlink(str(self.media / self.FRAME), str(self.media / 'timelapse_00-00-09-000.jpg'))
        token = self.login(self.app)
        for path in (
            '/api/media/timelapses/..%2Fsecrets.toml',
            '/api/media/timelapses/a.AVI',
            '/api/media/timelapses/.timelapse_videos.csv',
            '/api/media/frames/timelapse_00-00-09-000.jpg',
            '/api/media/frames/nope.jpg',
        ):
            with self.subTest(path=path):
                response = self.app.handle(self._req('GET', path, headers=self._auth(token)))
                self.assertEqual(response.status, 404)

    def test_build_requires_csrf_and_valid_fps(self):
        token = self.login(self.app)
        no_csrf = self.app.handle(self._req(
            'POST', '/api/media/timelapses/build', body={},
            headers=self._auth(token)))
        self.assertEqual(no_csrf.status, 403)
        csrf = self.sessions.csrf_for(token)
        bad_fps = self.app.handle(self._req(
            'POST', '/api/media/timelapses/build', body={'fps': 99},
            headers=self._auth(token, csrf=csrf)))
        self.assertEqual(bad_fps.status, 400)
        unknown = self.app.handle(self._req(
            'POST', '/api/media/timelapses/build', body={'nope': 1},
            headers=self._auth(token, csrf=csrf)))
        self.assertEqual(unknown.status, 400)

    def test_build_success_and_unavailable(self):
        token = self.login(self.app)
        csrf = self.sessions.csrf_for(token)
        response = self.app.handle(self._req(
            'POST', '/api/media/timelapses/build', body={'fps': 12},
            headers=self._auth(token, csrf=csrf)))
        self.assertEqual(response.status, 202)
        payload = json.loads(response.body)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['job']['state'], 'pending')
        self.assertEqual(self.build.starts, [12])

        no_manager = self._build_app(media_dir=str(self.media))
        token2 = self.login(no_manager)
        csrf2 = self.sessions.csrf_for(token2)
        response = no_manager.handle(self._req(
            'POST', '/api/media/timelapses/build', body={},
            headers=self._auth(token2, csrf=csrf2)))
        self.assertEqual(response.status, 503)

    def test_build_busy_and_gate_status_codes(self):
        token = self.login(self.app)
        csrf = self.sessions.csrf_for(token)
        cases = (
            ({'ok': False, 'error': 'a build is already running', 'code': 'busy', 'job_id': '0' * 32}, 409),
            ({'ok': False, 'error': 'insufficient free space for the build', 'code': 'low_space', 'job_id': ''}, 507),
            ({'ok': False, 'error': 'too many frames for one build', 'code': 'too_many_frames', 'job_id': ''}, 422),
        )
        for result, expected in cases:
            with self.subTest(code=result['code']):
                app = self._build_app(
                    media_dir=str(self.media), build_manager=_FakeBuildManager(result))
                token = self.login(app)
                csrf = self.sessions.csrf_for(token)
                response = app.handle(self._req(
                    'POST', '/api/media/timelapses/build', body={},
                    headers=self._auth(token, csrf=csrf)))
                self.assertEqual(response.status, expected)
                self.assertFalse(json.loads(response.body)['ok'])

    def test_job_status_known_unknown_and_unavailable(self):
        token = self.login(self.app)
        known = self.app.handle(self._req(
            'GET', '/api/media/jobs/' + '0' * 32, headers=self._auth(token)))
        self.assertEqual(known.status, 200)
        self.assertTrue(json.loads(known.body)['job']['state'])
        unknown = self.app.handle(self._req(
            'GET', '/api/media/jobs/' + '1' * 32, headers=self._auth(token)))
        self.assertEqual(unknown.status, 404)
        invalid = self.app.handle(self._req(
            'GET', '/api/media/jobs/not-a-job', headers=self._auth(token)))
        self.assertEqual(invalid.status, 404)

    def test_canary_and_path_never_returned(self):
        canary = 'CANARY-media-secret-77aa'
        (self.root / 'secrets.toml').write_text(config_schema.dumps_secrets({
            'prusa': {'token': canary},
            'admin': {'password_hash': ADMIN_HASH},
        }))
        token = self.login(self.app)
        responses = [
            self.app.handle(self._req('GET', '/api/media/timelapses', headers=self._auth(token))),
            self.app.handle(self._req('GET', '/api/media/frames', headers=self._auth(token))),
            self.app.handle(self._req(
                'GET', f'/api/media/timelapses/{self.VIDEO}', headers=self._auth(token))),
        ]
        for response in responses:
            self.assertNotIn(canary.encode(), response.body)
            self.assertNotIn(str(self.media).encode(), response.body)
        for response in responses:
            if response.file is not None:
                response.file.close()

    def test_job_status_is_global_authenticated_but_bounded(self):
        token_a = self.login(self.app)
        csrf = self.sessions.csrf_for(token_a)
        created = self.app.handle(self._req(
            'POST', '/api/media/timelapses/build', body={},
            headers=self._auth(token_a, csrf=csrf)))
        job_id = json.loads(created.body)['job']['id']
        # A second authenticated session can read the job (documented global
        # visibility) but the view is bounded and carries no path/secret.
        token_b = self.login(self.app)
        self.assertNotEqual(token_a, token_b)
        response = self.app.handle(self._req(
            'GET', f'/api/media/jobs/{job_id}', headers=self._auth(token_b)))
        self.assertEqual(response.status, 200)
        view = json.loads(response.body)['job']
        self.assertLessEqual(
            set(view), {'id', 'state', 'frames_total', 'frames_written',
                        'reason', 'started', 'finished'})
        self.assertNotIn(str(self.media), response.body.decode())

    def test_close_stops_the_build_manager(self):
        self.app.close()
        self.assertTrue(self.build.stopped)


# --------------------------------------------------------------------------- #
# System / update / diagnostics / reboot (WP-UI7; AC-14..AC-17)
# --------------------------------------------------------------------------- #

class FakeUpdateManager:
    def __init__(self, state=None, check=None, install=None):
        self._state = state if state is not None else {
            'state': 'update-available', 'installed_version': '1.0.0',
            'latest_version': '1.1.0', 'release_summary': 'notes',
            'in_progress': False, 'checking': False, 'installing': False,
            'last_check': 123.0, 'source_configured': True,
            'observed_at': NOW, 'fresh': True,
        }
        self._check = check if check is not None else {'ok': True, 'started': True}
        self._install = install if install is not None else {'ok': True, 'started': True}
        self.check_calls = 0
        self.install_calls = 0
        self.closed = False

    def state(self):
        return dict(self._state)

    def check(self):
        self.check_calls += 1
        return dict(self._check)

    def install(self):
        self.install_calls += 1
        return dict(self._install)

    def close(self):
        self.closed = True


class FakeDiagnosticsProvider:
    def __init__(self, document=None):
        self._document = document if document is not None else {
            'ok': True, 'available': True, 'state': 'ready',
            'text': 'boot ok\n', 'lines': 1, 'bytes': 8, 'truncated': False,
        }
        self.secrets = None
        self.stopped = False

    def __call__(self, secrets=()):
        self.secrets = tuple(secrets or ())
        return dict(self._document)

    def stop(self):
        self.stopped = True


class FakeReboot:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = 0

    def __call__(self):
        self.calls += 1
        # Mirrors privileged.PrivilegedResult: truthy exactly when ok.
        return privileged.PrivilegedResult(self.ok, '' if self.ok else 'failed')


class SystemRouteTests(AdminHttpTestBase):
    def _system_app(self, **overrides):
        kwargs = dict(
            release_identity_fn=lambda: {
                'version': '1.1.6', 'source_commit': 'abc1234',
                'os_suite': 'trixie', 'kernel_package': 'linux-image',
            },
            application_version_fn=lambda: '1.1.6',
            hostname_fn=lambda: 'pibuddycam-test',
        )
        kwargs.update(overrides)
        return self._build_app(**kwargs)

    def test_requires_authentication(self):
        response = self.app.handle(self.req('GET', '/api/system'))
        self.assertEqual(response.status, 401)

    def test_returns_bounded_system_facts(self):
        app = self._system_app()
        token = self.login(app)
        response = app.handle(self.req(
            'GET', '/api/system', headers=self.auth_headers(token)))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload['version']['release'], '1.1.6')
        self.assertEqual(payload['version']['source_commit'], 'abc1234')
        self.assertEqual(payload['network']['hostname'], 'pibuddycam-test')
        self.assertEqual(payload['provisioning']['state'], 'claimed')
        self.assertIn('enabled', payload['ssh'])
        self.assertNotIn('password', response.body.decode().lower())
        self.assertNotIn('token', response.body.decode().lower())

    def test_identity_failure_degrades_without_leaking(self):
        app = self._system_app(
            release_identity_fn=lambda: (_ for _ in ()).throw(RuntimeError('boom')),
            application_version_fn=lambda: (_ for _ in ()).throw(RuntimeError('boom')),
        )
        token = self.login(app)
        response = app.handle(self.req(
            'GET', '/api/system', headers=self.auth_headers(token)))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload['version']['release'], '')


class UpdateRouteTests(AdminHttpTestBase):
    def test_routes_require_authentication(self):
        for method, path in (
            ('GET', '/api/update'),
            ('POST', '/api/update/check'),
            ('POST', '/api/update/install'),
        ):
            response = self.app.handle(self.req(method, path, body={}))
            self.assertEqual(response.status, 401, path)

    def test_get_update_state_is_read_only(self):
        manager = FakeUpdateManager()
        app = self._build_app(update_manager=manager)
        token = self.login(app)
        response = app.handle(self.req(
            'GET', '/api/update', headers=self.auth_headers(token)))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload['latest_version'], '1.1.0')
        self.assertEqual(manager.check_calls, 0)
        self.assertEqual(manager.install_calls, 0)

    def test_get_update_unavailable_without_manager(self):
        token = self.login()
        response = self.app.handle(self.req(
            'GET', '/api/update', headers=self.auth_headers(token)))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertFalse(payload['available'])

    def test_check_requires_csrf(self):
        manager = FakeUpdateManager()
        app = self._build_app(update_manager=manager)
        token = self.login(app)
        response = app.handle(self.req(
            'POST', '/api/update/check', body={},
            headers=self.auth_headers(token)))
        self.assertEqual(response.status, 403)
        self.assertEqual(manager.check_calls, 0)

    def test_check_is_report_only_and_starts_no_install(self):
        manager = FakeUpdateManager()
        app = self._build_app(update_manager=manager)
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        response = app.handle(self.req(
            'POST', '/api/update/check', body={},
            headers=self.auth_headers(token, csrf=csrf)))
        self.assertEqual(response.status, 202)
        self.assertEqual(manager.check_calls, 1)
        self.assertEqual(manager.install_calls, 0)

    def test_check_rejects_browser_override_fields(self):
        manager = FakeUpdateManager()
        app = self._build_app(update_manager=manager)
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        for body in (
            {'url': 'https://evil.example/m.json'},
            {'manifest': 'x'},
            {'channel': 'alpha'},
            {'version': '9.9.9'},
            {'key': 'abc'},
            {'command': 'rm -rf /'},
        ):
            response = app.handle(self.req(
                'POST', '/api/update/check', body=body,
                headers=self.auth_headers(token, csrf=csrf)))
            self.assertEqual(response.status, 400, body)
        self.assertEqual(manager.check_calls, 0)

    def test_check_duplicate_is_409(self):
        manager = FakeUpdateManager(
            check={'ok': False, 'busy': True, 'reason': 'already running'})
        app = self._build_app(update_manager=manager)
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        response = app.handle(self.req(
            'POST', '/api/update/check', body={},
            headers=self.auth_headers(token, csrf=csrf)))
        self.assertEqual(response.status, 409)

    def test_check_unavailable_is_503(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        response = self.app.handle(self.req(
            'POST', '/api/update/check', body={},
            headers=self.auth_headers(token, csrf=csrf)))
        self.assertEqual(response.status, 503)

    def test_install_requires_fresh_reauth(self):
        manager = FakeUpdateManager()
        app = self._build_app(update_manager=manager)
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        response = app.handle(self.req(
            'POST', '/api/update/install', body={},
            headers=self.auth_headers(token, csrf=csrf)))
        self.assertEqual(response.status, 403)
        self.assertEqual(manager.install_calls, 0)

    def test_install_with_inline_password_invokes_fixed_path(self):
        manager = FakeUpdateManager()
        app = self._build_app(update_manager=manager)
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        response = app.handle(self.req(
            'POST', '/api/update/install', body={'password': ADMIN_PASSWORD},
            headers=self.auth_headers(token, csrf=csrf)))
        self.assertEqual(response.status, 202)
        self.assertEqual(manager.install_calls, 1)
        payload = json.loads(response.body)
        self.assertIn('warning', payload)
        self.assertIn('restart', payload['warning'].lower())

    def test_install_with_window_and_empty_body(self):
        manager = FakeUpdateManager()
        app = self._build_app(update_manager=manager)
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        self.assertEqual(app.handle(self.req(
            'POST', '/api/reauth', body={'password': ADMIN_PASSWORD},
            headers=self.auth_headers(token, csrf=csrf))).status, 200)
        response = app.handle(self.req(
            'POST', '/api/update/install', body={},
            headers=self.auth_headers(token, csrf=csrf)))
        self.assertEqual(response.status, 202)
        self.assertEqual(manager.install_calls, 1)

    def test_install_rejects_browser_override_fields(self):
        manager = FakeUpdateManager()
        app = self._build_app(update_manager=manager)
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        response = app.handle(self.req(
            'POST', '/api/update/install',
            body={'password': ADMIN_PASSWORD, 'url': 'https://evil.example'},
            headers=self.auth_headers(token, csrf=csrf)))
        self.assertEqual(response.status, 400)
        self.assertEqual(manager.install_calls, 0)

    def test_install_duplicate_is_409(self):
        manager = FakeUpdateManager(
            install={'ok': False, 'busy': True, 'reason': 'already installing'})
        app = self._build_app(update_manager=manager)
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        response = app.handle(self.req(
            'POST', '/api/update/install', body={'password': ADMIN_PASSWORD},
            headers=self.auth_headers(token, csrf=csrf)))
        self.assertEqual(response.status, 409)

    def test_close_closes_the_update_manager(self):
        manager = FakeUpdateManager()
        app = self._build_app(update_manager=manager)
        app.close()
        self.assertTrue(manager.closed)


class DiagnosticsRouteTests(AdminHttpTestBase):
    def test_requires_authentication(self):
        response = self.app.handle(self.req('GET', '/api/diagnostics'))
        self.assertEqual(response.status, 401)

    def test_returns_bounded_document_and_passes_secrets(self):
        provider = FakeDiagnosticsProvider()
        app = self._build_app(diagnostics_provider=provider)
        token = self.login(app)
        response = app.handle(self.req(
            'GET', '/api/diagnostics', headers=self.auth_headers(token)))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload['available'])
        self.assertEqual(payload['text'], 'boot ok\n')
        self.assertIsInstance(provider.secrets, tuple)

    def test_unavailable_without_provider(self):
        token = self.login()
        response = self.app.handle(self.req(
            'GET', '/api/diagnostics', headers=self.auth_headers(token)))
        self.assertEqual(response.status, 200)
        self.assertFalse(json.loads(response.body)['available'])

    def test_provider_exception_degrades(self):
        class Boom:
            def __call__(self, secrets=()):
                raise RuntimeError('boom')

            def stop(self):
                pass

        app = self._build_app(diagnostics_provider=Boom())
        token = self.login(app)
        response = app.handle(self.req(
            'GET', '/api/diagnostics', headers=self.auth_headers(token)))
        self.assertEqual(response.status, 200)
        self.assertFalse(json.loads(response.body)['available'])

    def test_close_stops_the_provider(self):
        provider = FakeDiagnosticsProvider()
        app = self._build_app(diagnostics_provider=provider)
        app.close()
        self.assertTrue(provider.stopped)


class RebootRouteTests(AdminHttpTestBase):
    def _reboot_app(self, reboot=None):
        return self._build_app(reboot_fn=reboot or FakeReboot())

    def _prime(self, app, token, csrf):
        self.assertEqual(app.handle(self.req(
            'POST', '/api/reauth', body={'password': ADMIN_PASSWORD},
            headers=self.auth_headers(token, csrf=csrf))).status, 200)

    def test_requires_authentication(self):
        response = self.app.handle(self.req(
            'POST', '/api/reboot', body={'confirm': True}))
        self.assertEqual(response.status, 401)

    def test_requires_fresh_window_not_inline_password(self):
        reboot = FakeReboot()
        app = self._reboot_app(reboot)
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        response = app.handle(self.req(
            'POST', '/api/reboot',
            body={'confirm': True, 'password': ADMIN_PASSWORD},
            headers=self.auth_headers(token, csrf=csrf)))
        self.assertEqual(response.status, 403)
        self.assertEqual(reboot.calls, 0)

    def test_requires_explicit_confirmation(self):
        reboot = FakeReboot()
        app = self._reboot_app(reboot)
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        self._prime(app, token, csrf)
        response = app.handle(self.req(
            'POST', '/api/reboot', body={},
            headers=self.auth_headers(token, csrf=csrf)))
        self.assertEqual(response.status, 400)
        self.assertEqual(reboot.calls, 0)

    def test_reboot_invokes_fixed_verb_once(self):
        reboot = FakeReboot()
        app = self._reboot_app(reboot)
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        self._prime(app, token, csrf)
        response = app.handle(self.req(
            'POST', '/api/reboot', body={'confirm': True},
            headers=self.auth_headers(token, csrf=csrf)))
        self.assertEqual(response.status, 200)
        self.assertEqual(reboot.calls, 1)
        self.assertIn('warning', json.loads(response.body))

    def test_reboot_is_rate_limited_with_retry_after(self):
        reboot = FakeReboot()
        app = self._reboot_app(reboot)
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        self._prime(app, token, csrf)
        first = app.handle(self.req(
            'POST', '/api/reboot', body={'confirm': True},
            headers=self.auth_headers(token, csrf=csrf), now=NOW))
        self.assertEqual(first.status, 200)
        second = app.handle(self.req(
            'POST', '/api/reboot', body={'confirm': True},
            headers=self.auth_headers(token, csrf=csrf), now=NOW))
        self.assertEqual(second.status, 429)
        self.assertIn('Retry-After', second.headers)
        self.assertEqual(reboot.calls, 1)
        later = NOW + device_control.DEFAULT_REBOOT_MIN_INTERVAL_SECONDS + 1
        third = app.handle(self.req(
            'POST', '/api/reboot', body={'confirm': True},
            headers=self.auth_headers(token, csrf=csrf), now=later))
        self.assertEqual(third.status, 200)
        self.assertEqual(reboot.calls, 2)

    def test_reboot_command_failure_is_not_reported_as_success(self):
        reboot = FakeReboot(ok=False)
        app = self._reboot_app(reboot)
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        self._prime(app, token, csrf)
        response = app.handle(self.req(
            'POST', '/api/reboot', body={'confirm': True},
            headers=self.auth_headers(token, csrf=csrf)))
        self.assertEqual(response.status, 500)

    def test_reboot_unavailable_without_callable(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        self._prime(self.app, token, csrf)
        response = self.app.handle(self.req(
            'POST', '/api/reboot', body={'confirm': True},
            headers=self.auth_headers(token, csrf=csrf)))
        self.assertEqual(response.status, 503)


# --------------------------------------------------------------------------- #
# Session-token helper shared with the aiohttp adapter's local-WebRTC upgrade
# --------------------------------------------------------------------------- #

class SessionTokenFromCookieTests(unittest.TestCase):
    def test_extracts_the_session_cookie_value(self):
        token = admin_http.session_token_from_cookie(f'{SESSION_COOKIE}=abc123')
        self.assertEqual(token, 'abc123')

    def test_extracts_among_other_cookies(self):
        token = admin_http.session_token_from_cookie(
            f'foo=bar; {SESSION_COOKIE}=abc123; baz=qux')
        self.assertEqual(token, 'abc123')

    def test_missing_cookie_header_returns_empty(self):
        for bad in ('', None, 123, []):
            with self.subTest(bad=bad):
                self.assertEqual(admin_http.session_token_from_cookie(bad), '')

    def test_header_without_session_cookie_returns_empty(self):
        self.assertEqual(admin_http.session_token_from_cookie('foo=bar'), '')

    def test_malformed_cookie_header_returns_empty(self):
        self.assertEqual(admin_http.session_token_from_cookie('\x00garbage'), '')


class ValidateSessionTokenTests(AdminHttpTestBase):
    def test_valid_token_from_login_validates(self):
        token = self.login()
        self.assertTrue(self.app.validate_session_token(token, now=NOW))

    def test_unknown_token_does_not_validate(self):
        self.assertFalse(self.app.validate_session_token('not-a-real-token', now=NOW))

    def test_empty_token_does_not_validate(self):
        self.assertFalse(self.app.validate_session_token('', now=NOW))

    def test_uses_the_app_clock_when_now_is_omitted(self):
        token = self.login()
        self.assertTrue(self.app.validate_session_token(token))

    def test_expired_session_does_not_validate(self):
        token = self.login()
        far_future = NOW + 10 * admin_auth.DEFAULT_IDLE_TTL
        self.assertFalse(self.app.validate_session_token(token, now=far_future))


if __name__ == '__main__':
    unittest.main()


# --------------------------------------------------------------------------- #
# Captive portal
# --------------------------------------------------------------------------- #

class CaptivePortalTests(AdminHttpTestBase):
    """Foreign-Host probes are redirected to the portal in setup mode only."""

    PROBES = (
        ('captive.apple.com', '/hotspot-detect.html'),
        ('connectivitycheck.gstatic.com', '/generate_204'),
        ('www.msftconnecttest.com', '/connecttest.txt'),
        ('detectportal.firefox.com', '/canonical.html'),
        ('example.org', '/'),
        ('pibuddycam-ddeeff.local', '/admin'),
    )

    def _setup_app(self):
        return self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
        )

    def test_os_probes_redirect_to_the_portal(self):
        app = self._setup_app()
        for host, path in self.PROBES:
            response = app.handle(self.req('GET', path, headers={'Host': host}))
            self.assertEqual(response.status, 302, host)
            self.assertEqual(response.headers['Location'], 'http://192.168.4.1/', host)
            self.assertEqual(
                response.headers['Cache-Control'], admin_http.NO_STORE_CACHE_CONTROL)
            self.assertEqual(response.body, b'')

    def test_portal_host_is_served_normally(self):
        app = self._setup_app()
        for host in ('192.168.4.1', '192.168.4.1:80', 'LOCALHOST:8080', '127.0.0.1'):
            response = app.handle(self.req('GET', '/setup', headers={'Host': host}))
            self.assertEqual(response.status, 200, host)

    def test_unknown_portal_path_redirects_but_api_stays_404(self):
        app = self._setup_app()
        response = app.handle(self.req(
            'GET', '/generate_204', headers={'Host': '192.168.4.1'}))
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers['Location'], '/')
        self.assertEqual(app.handle(self.req('GET', '/api/nope')).status, 404)
        self.assertEqual(app.handle(self.req('POST', '/nope', body={})).status, 404)

    def test_foreign_host_cannot_drive_the_wizard_api(self):
        app = self._setup_app()
        response = app.handle(self.req(
            'POST', '/setup/step/1', body={}, headers={'Host': 'evil.example'}))
        self.assertEqual(response.status, 421)

    def test_admin_mode_never_redirects(self):
        for host, path in self.PROBES:
            response = self.app.handle(self.req('GET', path, headers={'Host': host}))
            self.assertNotEqual(
                response.headers.get('Location'), 'http://192.168.4.1/', host)
        self.assertEqual(
            self.app.handle(self.req(
                'GET', '/generate_204', headers={'Host': 'example.org'})).status,
            404,
        )
