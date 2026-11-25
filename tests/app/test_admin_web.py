"""WP-UI1 (AC-1/AC-2/AC-3/AC-18, host AC-19): local admin web shell.

Stdlib-only. These tests never import ``aiohttp``; they drive the
:mod:`admin_http` policy core with transport-neutral :class:`Request` objects
and parse the packaged ``app/web/`` source with the standard
library. No network, subprocess, live device, or browser dependency is used.
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import admin_auth  # noqa: E402
import admin_http  # noqa: E402
import provisioning  # noqa: E402

WEB_DIR = PI_DIR / 'web'

ADMIN_PASSWORD = 'correct horse battery staple'
ADMIN_HASH = admin_auth.hash_password(ADMIN_PASSWORD)
SESSION_COOKIE = admin_auth.SESSION_COOKIE_NAME
NOW = 1000.0


def _make_request(method, path, body=None, headers=None, peer_ip='10.0.0.5', now=NOW):
    """Build a transport-neutral :class:`admin_http.Request`."""
    if isinstance(body, (dict, list)):
        raw = json.dumps(body).encode('utf-8')
        merged = {'Content-Type': 'application/json'}
    else:
        raw = b''
        merged = {}
    merged.update(headers or {})
    return admin_http.Request(method, path, {}, merged, raw, peer_ip, now)


def _build_app(**overrides):
    kwargs = dict(
        mode='admin',
        admin_hash=ADMIN_HASH,
        provisioning_state=provisioning.ProvisioningState(state='claimed'),
        web_dir=str(WEB_DIR),
    )
    kwargs.update(overrides)
    return admin_http.AdminApp(**kwargs)


def _parse_cookie(response):
    raw = response.headers.get('Set-Cookie', '')
    for part in raw.split(';'):
        name, _, value = part.strip().partition('=')
        if name == SESSION_COOKIE:
            return value
    return ''


# --------------------------------------------------------------------------- #
# WCAG contrast helpers (AC-3)                                                 #
# --------------------------------------------------------------------------- #















# --------------------------------------------------------------------------- #
# Shell                                                                        #
# --------------------------------------------------------------------------- #

class AdminShellTests(unittest.TestCase):
    """AC-1: ``/admin`` is a login shell; device data stays authenticated."""

    def setUp(self):
        self.app = _build_app()

    def test_admin_serves_the_application_shell(self):
        response = self.app.handle(_make_request('GET', '/admin'))
        self.assertEqual(response.status, 200)
        self.assertTrue(
            response.headers['Content-Type'].startswith('text/html'))
        html = response.body.decode('utf-8')
        self.assertIn('<!doctype html>', html.lower())
        self.assertIn('lang="en"', html)
        self.assertIn('name="viewport"', html)
        self.assertIn('id="login-form"', html)
        self.assertIn('id="app-view"', html)
        self.assertIn('id="logout"', html)

    def test_shell_has_no_unresolved_placeholders(self):
        html = self.app.handle(_make_request('GET', '/admin')).body.decode('utf-8')
        self.assertNotIn('__ASSET_VERSION__', html)
        self.assertNotIn('__TRUSTED_LAN_NOTICE__', html)

    def test_shell_carries_the_trusted_lan_notice(self):
        html = self.app.handle(_make_request('GET', '/admin')).body.decode('utf-8')
        for marker in ('ONVIF', '/snapshot.jpg', '8554', '8555'):
            self.assertIn(marker, html)

    def test_shell_carries_the_non_affiliation_notice(self):
        html = self.app.handle(_make_request('GET', '/admin')).body.decode('utf-8')
        self.assertIn('Not affiliated with or endorsed by Prusa Research', html)
        self.assertIn('Independent, community-developed project', html)

    def test_shell_has_no_inline_code(self):
        html = self.app.handle(_make_request('GET', '/admin')).body.decode('utf-8')
        self.assertNotIn('<style', html.lower())
        self.assertIsNone(
            re.search(r'<script(?![^>]*\bsrc=)', html, re.IGNORECASE),
            'the shell must not contain an inline script',
        )
        self.assertIsNone(
            re.search(r'\son[a-z]+\s*=', html, re.IGNORECASE),
            'the shell must not use inline event handlers',
        )

    def test_shell_references_only_allowlisted_assets(self):
        html = self.app.handle(_make_request('GET', '/admin')).body.decode('utf-8')
        referenced = set(re.findall(r'/assets/([A-Za-z0-9._-]+)', html))
        self.assertTrue(referenced)
        self.assertTrue(referenced <= set(admin_http.ASSET_ALLOWLIST))
        for name in ('app.css', 'app.js', 'favicon.svg'):
            self.assertIn(name, referenced)

    def test_served_modules_carry_the_content_version(self):
        html = self.app.handle(_make_request('GET', '/admin')).body.decode('utf-8')
        version = re.search(r'/assets/app\.js\?v=([0-9a-f]+)', html).group(1)
        for name in sorted(n for n in admin_http.ASSET_ALLOWLIST if n.endswith('.js')):
            response = self.app.handle(_make_request('GET', f'/assets/{name}'))
            self.assertEqual(response.status, 200, name)
            body = response.body.decode('utf-8')
            self.assertNotIn('__ASSET_VERSION__', body, name)
            for spec in re.findall(r"from '\./[A-Za-z0-9._-]+\.js\?v=([^']+)'", body):
                self.assertEqual(spec, version, name)
            self.assertEqual(response.headers['Content-Type'], 'text/javascript; charset=utf-8')

    def test_asset_version_changes_when_a_module_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            for path in WEB_DIR.iterdir():
                (Path(tmp) / path.name).write_bytes(path.read_bytes())
            first = admin_http.AdminApp(web_dir=tmp)._asset_version()
            (Path(tmp) / 'network.js').write_text('export {};\n', encoding='utf-8')
            self.assertNotEqual(first, admin_http.AdminApp(web_dir=tmp)._asset_version())

    def test_shell_is_public_in_both_modes(self):
        for mode in ('admin', 'setup'):
            with self.subTest(mode=mode):
                app = _build_app(mode=mode)
                response = app.handle(_make_request('GET', '/admin'))
                self.assertEqual(response.status, 200)
                self.assertIn('id="login-form"', response.body.decode('utf-8'))

    def test_shell_does_not_embed_provisioning_state(self):
        html = self.app.handle(_make_request('GET', '/admin')).body.decode('utf-8')
        self.assertNotIn('provisioning_state', html)
        self.assertNotIn('claimed', html)

    def test_missing_shell_falls_back_to_a_minimal_login_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _build_app(web_dir=tmp)
            with self.assertLogs('pibuddycam.admin_http', level='WARNING'):
                response = app.handle(_make_request('GET', '/admin'))
        self.assertEqual(response.status, 200)
        html = response.body.decode('utf-8')
        self.assertIn('PiBuddyCam', html)
        self.assertIn('Trusted LAN only', html)


# --------------------------------------------------------------------------- #
# Static assets                                                                #
# --------------------------------------------------------------------------- #


class AdminAssetTests(unittest.TestCase):
    """AC-2: only allowlisted local assets, safe and correctly typed."""

    def setUp(self):
        self.app = _build_app()

    def test_every_allowlisted_asset_is_served_with_mime(self):
        for name, content_type in admin_http.ASSET_ALLOWLIST.items():
            with self.subTest(asset=name):
                response = self.app.handle(
                    _make_request('GET', '/assets/' + name))
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers['Content-Type'], content_type)
                self.assertTrue(response.body)

    def test_assets_carry_cache_etag_and_nosniff(self):
        for name in admin_http.ASSET_ALLOWLIST:
            with self.subTest(asset=name):
                response = self.app.handle(
                    _make_request('GET', '/assets/' + name))
                self.assertEqual(
                    response.headers['Cache-Control'],
                    admin_http.ASSET_CACHE_CONTROL,
                )
                self.assertTrue(response.headers['ETag'].startswith('"'))
                self.assertEqual(
                    response.headers['X-Content-Type-Options'], 'nosniff')

    def test_matching_etag_returns_a_bodyless_304(self):
        response = self.app.handle(_make_request('GET', '/assets/app.css'))
        etag = response.headers['ETag']
        cached = self.app.handle(_make_request(
            'GET', '/assets/app.css', headers={'If-None-Match': etag}))
        self.assertEqual(cached.status, 304)
        self.assertEqual(cached.body, b'')
        self.assertEqual(cached.headers['ETag'], etag)

    def test_unknown_asset_is_404(self):
        for name in ('nope.txt', 'app.css.bak', 'index.html', 'icons.svg'):
            with self.subTest(asset=name):
                response = self.app.handle(
                    _make_request('GET', '/assets/' + name))
                self.assertEqual(response.status, 404)

    def test_traversal_and_dot_names_are_rejected(self):
        for path in (
            '/assets/..',
            '/assets/.',
            '/assets/../secrets.toml',
            '/assets/..%2fsecrets.toml',
            '/assets/app.css/../../etc/passwd',
            '/assets/%2e%2e%2fapp.css',
            '/assets/',
        ):
            with self.subTest(path=path):
                response = self.app.handle(_make_request('GET', path))
                self.assertEqual(response.status, 404)

    def test_svg_asset_gets_a_restrictive_csp(self):
        response = self.app.handle(
            _make_request('GET', '/assets/favicon.svg'))
        self.assertEqual(
            response.headers['Content-Security-Policy'], admin_http.SVG_CSP)
        self.assertEqual(admin_http.SVG_CSP, "default-src 'none'")

    def test_missing_asset_file_is_404_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _build_app(web_dir=tmp)
            response = app.handle(
                _make_request('GET', '/assets/app.css'))
        self.assertEqual(response.status, 404)

    def test_asset_version_changes_with_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = WEB_DIR / 'app.css'
            Path(tmp, 'app.css').write_text(
                source.read_text(encoding='utf-8'), encoding='utf-8')
            first = _build_app(web_dir=tmp)._asset_version()
            Path(tmp, 'app.css').write_text('/* changed */\n', encoding='utf-8')
            second = _build_app(web_dir=tmp)._asset_version()
        self.assertNotEqual(first, second)
        self.assertRegex(first, r'^[0-9a-f]{12}$')

    def test_shell_asset_query_matches_the_served_version(self):
        html = self.app.handle(_make_request('GET', '/admin')).body.decode('utf-8')
        version = self.app._asset_version()
        self.assertIn(f'/assets/app.css?v={version}', html)
        self.assertIn(f'/assets/app.js?v={version}', html)


# --------------------------------------------------------------------------- #
# Security headers                                                             #
# --------------------------------------------------------------------------- #

class AdminSecurityHeaderTests(unittest.TestCase):
    """AC-2/AC-3: CSP and baseline headers on every response class."""

    def setUp(self):
        self.app = _build_app()

    def _assert_baseline(self, response):
        for name, value in admin_http.SECURITY_HEADERS.items():
            self.assertEqual(response.headers.get(name), value, name)

    def test_baseline_headers_on_html_json_text_and_redirect(self):
        self._assert_baseline(self.app.handle(_make_request('GET', '/admin')))
        self._assert_baseline(self.app.handle(_make_request('GET', '/api/status')))
        self._assert_baseline(self.app.handle(_make_request('GET', '/nope')))
        self._assert_baseline(self.app.handle(_make_request('GET', '/')))
        self._assert_baseline(
            self.app.handle(_make_request('GET', '/assets/app.js')))

    def test_html_csp_is_local_only(self):
        response = self.app.handle(_make_request('GET', '/admin'))
        csp = response.headers['Content-Security-Policy']
        self.assertEqual(csp, admin_http.HTML_CSP)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        # The live monitor renders an authenticated frame through
        # URL.createObjectURL, so the shell must allow blob: images or every
        # monitor frame is silently blocked by CSP.
        self.assertIn("img-src 'self' data: blob:", csp)
        self.assertNotIn("'unsafe-inline'", csp)
        self.assertNotIn('http:', csp)
        self.assertNotIn('https:', csp)


# --------------------------------------------------------------------------- #
# Session endpoint (AC-1)                                                      #
# --------------------------------------------------------------------------- #

class AdminSessionEndpointTests(unittest.TestCase):
    """AC-1/AC-18: the session probe is authenticated and CSRF-aware."""

    def setUp(self):
        self.app = _build_app()

    def _login(self):
        response = self.app.handle(_make_request(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD}))
        self.assertEqual(response.status, 200, response.body)
        token = _parse_cookie(response)
        csrf = json.loads(response.body)['csrf']
        return token, csrf

    def test_session_requires_authentication(self):
        response = self.app.handle(_make_request('GET', '/api/session'))
        self.assertEqual(response.status, 401)

    def test_session_returns_mode_and_csrf_after_login(self):
        token, csrf = self._login()
        response = self.app.handle(_make_request(
            'GET', '/api/session',
            headers={'Cookie': f'{SESSION_COOKIE}={token}'}))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['mode'], 'admin')
        self.assertEqual(payload['csrf'], csrf)

    def test_session_is_dead_after_logout(self):
        token, csrf = self._login()
        cookie = {'Cookie': f'{SESSION_COOKIE}={token}'}
        logout = self.app.handle(_make_request(
            'POST', '/api/logout',
            headers={**cookie, 'X-CSRF-Token': csrf}))
        self.assertEqual(logout.status, 200)
        response = self.app.handle(_make_request(
            'GET', '/api/session', headers=cookie))
        self.assertEqual(response.status, 401)

    def test_session_never_returns_a_cookie_value(self):
        token, _ = self._login()
        body = self.app.handle(_make_request(
            'GET', '/api/session',
            headers={'Cookie': f'{SESSION_COOKIE}={token}'})).body.decode('utf-8')
        self.assertNotIn(token, body)

    def test_route_parity_declares_the_new_routes(self):
        routes = {
            (route.method, route.pattern.pattern)
            for route in admin_http.AdminApp()._routes
        }
        methods_by_path = {}
        for method, pattern in routes:
            methods_by_path.setdefault(pattern, set()).add(method)
        self.assertIn(r'^/assets/(?P<name>[^/]+)$', methods_by_path)
        self.assertEqual(methods_by_path[r'^/assets/(?P<name>[^/]+)$'], {'GET'})
        self.assertEqual(methods_by_path[r'^/api/session$'], {'GET'})


# --------------------------------------------------------------------------- #
# Design system source contract (AC-3)                                         #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Overview dashboard UI (WP-UI2; AC-14/AC-17)                                 #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Camera + Integrations forms (WP-UI3/WP-UI4; AC-5..AC-9)                      #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Local monitor (WP-UI5; AC-10/AC-11)                                          #
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Packaging (AC-2)                                                             #
# --------------------------------------------------------------------------- #

class AdminAssetPackagingTests(unittest.TestCase):
    """AC-2: the factory tree and signed application bundle ship the assets."""

    def test_repo_web_tree_has_shell_and_all_allowlisted_assets(self):
        self.assertTrue((WEB_DIR / admin_http.SHELL_FILE).is_file())
        for name in admin_http.ASSET_ALLOWLIST:
            self.assertTrue((WEB_DIR / name).is_file(), name)


# --------------------------------------------------------------------------- #
# System view (WP-UI7; AC-14..AC-17)                                           #
# --------------------------------------------------------------------------- #


if __name__ == '__main__':
    unittest.main()
