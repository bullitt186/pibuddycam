"""Source-structure checks for admin_settings: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_admin_settings.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
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
        response = app.handle(
            _make_request('POST', '/api/login', body={'password': ADMIN_PASSWORD})
        )
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

    def test_admin_core_never_writes_state_json(self):
        source = (PI_DIR / 'admin_http.py').read_text(encoding='utf-8')
        # No durable-state writer is imported or called from the admin core; the
        # only settings mutation path is the injected runtime action.
        self.assertNotIn('settings_store', source)
        self.assertNotIn('save_persisted_state', source)
        self.assertNotIn('persistable_state', source)
        self.assertIn('self._settings_actions', source)


if __name__ == '__main__':
    unittest.main()
