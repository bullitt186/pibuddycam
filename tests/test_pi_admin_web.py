"""WP-UI1 (AC-1/AC-2/AC-3/AC-18, host AC-19): local admin web shell.

Stdlib-only. These tests never import ``aiohttp``; they drive the
:mod:`admin_http` policy core with transport-neutral :class:`Request` objects
and parse the packaged ``pi-impersonator/web/`` source with the standard
library. No network, subprocess, live device, or browser dependency is used.
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import admin_auth  # noqa: E402
import admin_http  # noqa: E402
import provisioning  # noqa: E402

WEB_DIR = PI_DIR / 'web'
INSTALLER = PI_DIR.parent / 'image' / 'assets' / 'install-factory-app.sh'
MAKE_APP_RELEASE = PI_DIR.parent / 'image' / 'scripts' / 'make-app-release.sh'

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

def _css_color_tokens(text):
    """Return ``{token: '#rrggbb'}`` for hex-valued custom properties."""
    return {
        match.group(1).lower(): match.group(2).lower()
        for match in re.finditer(
            r'--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})', text
        )
    }


def _srgb_to_linear(channel):
    channel = channel / 255.0
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return (
        0.2126 * _srgb_to_linear(r)
        + 0.7152 * _srgb_to_linear(g)
        + 0.0722 * _srgb_to_linear(b)
    )


def _contrast_ratio(first, second):
    a, b = _relative_luminance(first), _relative_luminance(second)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def _strip_js_comments(text):
    """Remove ``/* ... */`` and ``// ...`` comments so tests read code only."""
    without_block = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    return re.sub(r'^\s*//.*$', '', without_block, flags=re.MULTILINE)


def _function_body(code, name):
    """Return the brace-balanced body of ``function name(...) { ... }``.

    ``code`` is expected to have comments stripped first. Template-literal
    interpolations (``${...}``) are balanced, so brace counting stays correct.
    """
    match = re.search(r'\bfunction\s+' + re.escape(name) + r'\s*\(', code)
    if match is None:
        raise AssertionError(f'function {name} not found')
    brace = code.index('{', match.end())
    depth = 0
    for index in range(brace, len(code)):
        if code[index] == '{':
            depth += 1
        elif code[index] == '}':
            depth -= 1
            if depth == 0:
                return code[brace + 1:index]
    raise AssertionError(f'function {name} body is not balanced')


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
            with self.assertLogs('prusa-cam.admin_http', level='WARNING'):
                response = app.handle(_make_request('GET', '/admin'))
        self.assertEqual(response.status, 200)
        html = response.body.decode('utf-8')
        self.assertIn('Buddy3D Camera', html)
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

class AdminDesignSystemTests(unittest.TestCase):
    """AC-3: original, accessible, local-only Prusa-adjacent design tokens."""

    def setUp(self):
        self.css = (WEB_DIR / 'app.css').read_text(encoding='utf-8')
        self.html = (WEB_DIR / 'index.html').read_text(encoding='utf-8')
        self.js = (WEB_DIR / 'app.js').read_text(encoding='utf-8')

    def test_web_tree_has_no_unknown_files(self):
        files = {path.name for path in WEB_DIR.iterdir() if path.is_file()}
        self.assertEqual(
            files, set(admin_http.ASSET_ALLOWLIST) | {admin_http.SHELL_FILE})

    def test_css_is_local_only(self):
        self.assertNotIn('@import', self.css)
        for marker in ('http://', 'https://', '//cdn', 'fonts.googleapis',
                       'fonts.gstatic', 'cdn.jsdelivr', 'unpkg.com'):
            self.assertNotIn(marker, self.css, marker)

    def test_html_and_js_are_local_only(self):
        for marker in ('http://', 'https://', '//cdn', 'google-analytics',
                       'gtag(', 'fonts.googleapis'):
            self.assertNotIn(marker, self.html, marker)
        # The JS builds same-host local-access URLs (http://<host>/snapshot.jpg),
        # which is not an external load; no external host/script marker may appear.
        for marker in ('//cdn', 'google-analytics', 'gtag(', 'fonts.googleapis',
                       'cdn.jsdelivr', 'unpkg.com', 'https://'):
            self.assertNotIn(marker, self.js, marker)
        for line in self.js.splitlines():
            if 'http://' in line:
                self.assertIn('${host}', line, line)

    def test_no_copied_prusa_assets(self):
        blob = (self.css + self.html + self.js).lower()
        for marker in ('prusa-logo', 'prusa_logo', 'prusa3d.com', 'logo.svg',
                       'connect.prusa3d.com'):
            self.assertNotIn(marker, blob, marker)

    def test_touch_targets_and_focus_are_visible(self):
        self.assertIn('--target: 44px', self.css)
        self.assertIn('min-height: var(--target)', self.css)
        self.assertIn(':focus-visible', self.css)
        self.assertIn('outline: 3px solid var(--focus)', self.css)

    def test_reduced_motion_is_respected(self):
        self.assertIn('prefers-reduced-motion: reduce', self.css)
        block = self.css.split('prefers-reduced-motion: reduce', 1)[1]
        self.assertIn('animation-duration', block)
        self.assertIn('transition-duration', block)

    def test_responsive_layout_covers_narrow_screens(self):
        self.assertIn('@media (max-width: 760px)', self.css)
        self.assertIn('@media (max-width: 380px)', self.css)
        self.assertIn('grid-template-areas', self.css)

    def test_documented_palette_meets_wcag_aa(self):
        tokens = _css_color_tokens(self.css)
        for name in (
            'bg', 'surface', 'ink', 'ink-muted', 'nav', 'nav-ink', 'nav-muted',
            'accent-strong', 'accent-on-dark', 'accent-ink', 'success',
            'warning', 'danger',
        ):
            self.assertIn(name, tokens, f'missing design token --{name}')
        pairs = (
            ('ink', 'bg'),
            ('ink-muted', 'bg'),
            ('accent-strong', 'bg'),
            ('accent-ink', 'accent-strong'),
            ('nav-ink', 'nav'),
            ('nav-muted', 'nav'),
            ('accent-on-dark', 'nav'),
            ('success', 'surface'),
            ('warning', 'surface'),
            ('danger', 'surface'),
        )
        for foreground, background in pairs:
            with self.subTest(pair=f'{foreground}/{background}'):
                ratio = _contrast_ratio(tokens[foreground], tokens[background])
                self.assertGreaterEqual(
                    round(ratio, 2), 4.5,
                    f'--{foreground} on --{background} is {ratio:.2f}:1',
                )

    def test_js_keeps_csrf_in_memory_only(self):
        code = _strip_js_comments(self.js)
        for storage in ('localStorage', 'sessionStorage', 'document.cookie'):
            self.assertNotIn(storage, code, storage)
        self.assertIn('X-CSRF-Token', code)

    def test_js_handles_session_expiry(self):
        self.assertIn('/api/session', self.js)
        self.assertIn('handleExpired', self.js)
        self.assertIn('result.status === 401', self.js)


# --------------------------------------------------------------------------- #
# Overview dashboard UI (WP-UI2; AC-14/AC-17)                                 #
# --------------------------------------------------------------------------- #

class AdminOverviewDashboardTests(unittest.TestCase):
    """AC-14: the Overview view polls the authenticated dashboard."""

    def setUp(self):
        self.html = (WEB_DIR / 'index.html').read_text(encoding='utf-8')
        self.js = (WEB_DIR / 'app.js').read_text(encoding='utf-8')
        self.css = (WEB_DIR / 'app.css').read_text(encoding='utf-8')
        self.app = _build_app()

    def test_overview_has_state_and_content_hooks(self):
        for marker in (
            'id="overview-state"', 'id="overview-content"',
            'id="overview-freshness"', 'id="overview-status"',
            'id="metric-resolution"', 'id="metric-quality"',
            'id="metric-wifi"', 'id="metric-temp"', 'id="metric-uptime"',
            'id="metric-storage"', 'id="metric-version"',
        ):
            self.assertIn(marker, self.html, marker)

    def test_overview_placeholder_is_replaced(self):
        html = self.app.handle(_make_request('GET', '/admin')).body.decode('utf-8')
        self.assertNotIn('Nothing to show yet', html)
        self.assertIn('id="overview-status"', html)

    def test_overview_carries_the_trusted_lan_notice(self):
        html = self.app.handle(_make_request('GET', '/admin')).body.decode('utf-8')
        self.assertIn('Trusted LAN only', html)
        self.assertIn('port-forward', html)

    def test_js_polls_the_dashboard(self):
        self.assertIn("fetch('/api/dashboard'", self.js)
        self.assertIn('/api/dashboard', self.js)

    def test_js_is_visibility_aware(self):
        self.assertIn('visibilitychange', self.js)
        self.assertIn('DASHBOARD_INTERVAL_VISIBLE', self.js)
        self.assertIn('DASHBOARD_INTERVAL_HIDDEN', self.js)
        self.assertIn('document.visibilityState', self.js)

    def test_js_aborts_stale_requests(self):
        self.assertIn('AbortController', self.js)
        self.assertIn('abort()', self.js)
        self.assertIn('AbortError', self.js)

    def test_js_handles_session_expiry_on_the_dashboard(self):
        code = _strip_js_comments(self.js)
        self.assertIn("response.status === 401", code)
        self.assertIn('handleExpired()', code)

    def test_js_stops_polling_on_login(self):
        code = _strip_js_comments(self.js)
        show_login = code.split('function showLogin', 1)[1].split('function ', 1)[0]
        self.assertIn('stopDashboardPolling', show_login)

    def test_js_has_loading_degraded_and_error_states(self):
        for marker in (
            'renderDashboardLoading', 'renderDashboardError',
            'renderDashboard', 'setOverviewMessage', 'clearOverviewMessage',
        ):
            self.assertIn(marker, self.js, marker)
        self.assertIn("source === 'unavailable'", self.js)
        self.assertIn("source === 'stale'", self.js)

    def test_js_renders_chips_metrics_and_freshness(self):
        for marker in (
            'renderStatusChips', 'renderMetrics', 'renderFreshness',
            'formatBytes', 'formatDuration', 'formatAge',
        ):
            self.assertIn(marker, self.js, marker)

    def test_css_defines_overview_and_chip_styles(self):
        for marker in (
            '.overview', '.metrics', '.metric__value', '.status-chips',
            '.overview-state', '.chip--warn', '.chip--error', '.chip--muted',
        ):
            self.assertIn(marker, self.css, marker)

    def test_overview_poll_is_read_only_but_camera_mutations_route_through_api(self):
        # WP-UI2 kept the Overview read-only; WP-UI3 adds settings mutations that
        # go exclusively through PATCH /api/settings (never a direct state write).
        code = _strip_js_comments(self.js)
        self.assertIn("method: 'PATCH'", code)
        self.assertIn('/api/settings', code)
        self.assertNotIn('state.json', code)
        self.assertNotIn('settings_store', code)
        self.assertIn("method: 'PUT'", code)
        self.assertIn('/api/integrations/mqtt', code)
        self.assertIn('/api/integrations/prusa', code)


# --------------------------------------------------------------------------- #
# Camera + Integrations forms (WP-UI3/WP-UI4; AC-5..AC-9)                      #
# --------------------------------------------------------------------------- #

class AdminSettingsIntegrationsUiTests(unittest.TestCase):
    """AC-5..AC-9: the Camera and Integrations views are wired to the APIs."""

    def setUp(self):
        self.html = (WEB_DIR / 'index.html').read_text(encoding='utf-8')
        self.js = (WEB_DIR / 'app.js').read_text(encoding='utf-8')
        self.css = (WEB_DIR / 'app.css').read_text(encoding='utf-8')

    def test_camera_view_has_a_form_for_every_confirmed_setting(self):
        for field in (
            'camera_name', 'quality', 'snapshot_upload_enabled',
            'snapshot_interval', 'timelapse_enabled', 'timelapse_interval',
            'timelapse_fps', 'rtsp_mode', 'webrtc_mode',
        ):
            self.assertIn(f'data-setting="{field}"', self.html, field)

    def test_camera_view_explains_unsupported_hardware_non_interactively(self):
        self.assertIn('Unsupported hardware', self.html)
        self.assertIn('IR/light', self.html)
        self.assertIn('speaker', self.html)
        self.assertIn('fan', self.html)
        self.assertIn('motor', self.html)
        # No interactive control for absent hardware.
        for marker in ('name="ir"', 'name="light"', 'name="speaker"', 'name="fan"'):
            self.assertNotIn(marker, self.html, marker)

    def test_js_applies_authoritative_settings_and_restores_on_rejection(self):
        code = _strip_js_comments(self.js)
        for marker in (
            'applySettings', 'submitSettingForm', 'readSettingValue',
            'setFormBusy', 'setFormStatus',
        ):
            self.assertIn(marker, code, marker)
        # A rejection still converges on the returned authoritative snapshot.
        self.assertIn('data.settings', code)
        self.assertIn("method: 'PATCH'", code)

    def test_js_disables_submission_while_pending(self):
        code = _strip_js_comments(self.js)
        self.assertIn('setFormBusy(form, true)', code)
        self.assertIn('button.disabled = busy', code)

    def test_js_refresh_converges_with_dashboard_settings(self):
        code = _strip_js_comments(self.js)
        self.assertIn('applySettings(data.settings)', code)
        self.assertIn('LAST_DASHBOARD', code)
        self.assertIn('syncCameraView', code)

    def test_integrations_view_has_prusa_and_mqtt_forms(self):
        for marker in (
            'id="prusa-form"', 'id="mqtt-form"', 'id="prusa-token"',
            'id="prusa-fingerprint"', 'id="prusa-clear-token"',
            'id="mqtt-uri"', 'id="mqtt-username"', 'id="mqtt-password"',
            'id="mqtt-clear-username"', 'id="mqtt-clear-password"',
            'id="mqtt-test"', 'id="mqtt-save-anyway"', 'id="mqtt-effective"',
            'id="mqtt-runtime"', 'id="local-onvif"', 'id="local-ha-rtsp"',
        ):
            self.assertIn(marker, self.html, marker)

    def test_integrations_js_requires_test_before_save_with_explicit_override(self):
        code = _strip_js_comments(self.js)
        self.assertIn("mqttTestState", code)
        self.assertIn("mqttTestState !== 'ok'", code)
        self.assertIn('mqttSaveAnyway', code)
        self.assertIn('/api/mqtt/test', code)
        self.assertIn('/api/integrations/mqtt', code)

    def test_integrations_js_requires_reauth_for_credential_saves(self):
        code = _strip_js_comments(self.js)
        self.assertIn('requestReauth', code)
        self.assertIn('/api/reauth', code)
        self.assertIn('reauth-dialog', self.html)
        self.assertIn('id="reauth-password"', self.html)
        self.assertIn('/api/integrations/prusa', code)

    def test_submit_mqtt_reauthenticates_before_the_put(self):
        code = _strip_js_comments(self.js)
        body = _function_body(code, 'submitMqtt')
        self.assertIn('requestReauth', body)
        self.assertIn('/api/integrations/mqtt', body)
        # Re-auth must be confirmed before the save request is issued.
        self.assertLess(body.index('requestReauth'), body.index('/api/integrations/mqtt'))
        # A cancelled/failed re-auth must surface an error and stop the save.
        self.assertIn('if (!confirmed)', body)
        self.assertIn('Re-authentication is required', body)
        # The MQTT save body is the broker form only: the admin re-auth password
        # field must never be read into it.
        self.assertIn('collectMqttBody()', body)
        for marker in ('reauth-password', 'els.reauthPassword', 'reauthPassword'):
            self.assertNotIn(marker, body, marker)

    def test_submit_mqtt_clears_typed_credentials_and_test_state(self):
        code = _strip_js_comments(self.js)
        body = _function_body(code, 'submitMqtt')
        self.assertIn("els.mqttUsername.value = ''", body)
        self.assertIn("els.mqttPw.value = ''", body)
        self.assertIn('invalidateMqttTest()', body)

    def test_collect_mqtt_body_never_includes_the_admin_password(self):
        code = _strip_js_comments(self.js)
        body = _function_body(code, 'collectMqttBody')
        for marker in ('reauth-password', 'els.reauthPassword', 'reauthPassword'):
            self.assertNotIn(marker, body, marker)
        # The broker password field is the only ``password`` it may read.
        self.assertIn("value('mqtt-password')", body)

    def test_mqtt_test_uses_stored_credential_flags_only_when_configured(self):
        code = _strip_js_comments(self.js)
        body = _function_body(code, 'collectMqttTestBody')
        self.assertIn('use_stored_username', body)
        self.assertIn('use_stored_password', body)
        self.assertIn('MQTT_CONFIGURED.username', body)
        self.assertIn('MQTT_CONFIGURED.password', body)
        self.assertIn("checked('mqtt-clear-username')", body)
        self.assertIn("checked('mqtt-clear-password')", body)

    def test_mqtt_test_success_is_invalidated_when_inputs_change(self):
        code = _strip_js_comments(self.js)
        # A canonical signature covers every effective tested/saved input ...
        signature = _function_body(code, 'mqttInputSignature')
        for field in ('mqtt-uri', 'mqtt-username', 'mqtt-password', 'mqtt-ca-file',
                      'mqtt-clear-username', 'mqtt-clear-password'):
            self.assertIn(field, signature, field)
        # ... and an edit drops both the success and the save-anyway override.
        invalidate = _function_body(code, 'invalidateMqttTest')
        self.assertIn("mqttTestState = 'untested'", invalidate)
        self.assertIn('mqttTestedSignature = null', invalidate)
        self.assertIn('mqttSaveAnyway.checked = false', invalidate)
        self.assertIn("addEventListener('input', invalidateMqttTest)", code)
        self.assertIn("addEventListener('change', invalidateMqttTest)", code)
        # The submit path re-checks the signature so a missed event is caught.
        submit = _function_body(code, 'submitMqtt')
        self.assertIn('mqttTestedSignature !== mqttInputSignature()', submit)
        # A successful test records the signature it validated.
        test = _function_body(code, 'testMqtt')
        self.assertIn('mqttTestedSignature = mqttInputSignature()', test)

    def test_mqtt_integration_reload_resets_the_test_state(self):
        code = _strip_js_comments(self.js)
        render = _function_body(code, 'renderIntegrations')
        self.assertIn('MQTT_CONFIGURED.username', render)
        self.assertIn('MQTT_CONFIGURED.password', render)
        self.assertIn('invalidateMqttTest()', render)

    def test_integrations_js_never_renders_a_stored_secret(self):
        code = _strip_js_comments(self.js)
        self.assertNotIn('token_configured ?', code)
        # The token/fingerprint inputs are always blank replacement fields.
        self.assertIn('prusa-token-state', code)
        self.assertIn('username_configured', code)
        self.assertIn('password_configured', code)

    def test_css_defines_settings_and_integration_components(self):
        for marker in (
            '.setting-form', '.segmented', '.form-status', '.form-status--ok',
            '.form-status--error', '.field__hint', '.check', '.topic-preview',
            '.local-access', '.dialog', '.notice--warn',
        ):
            self.assertIn(marker, self.css, marker)

    def test_js_keeps_csrf_in_memory_for_mutations(self):
        code = _strip_js_comments(self.js)
        for storage in ('localStorage', 'sessionStorage', 'document.cookie'):
            self.assertNotIn(storage, code, storage)
        self.assertIn('csrf: true', code)



# --------------------------------------------------------------------------- #
# Local monitor (WP-UI5; AC-10/AC-11)                                          #
# --------------------------------------------------------------------------- #

class AdminLiveMonitorUiTests(unittest.TestCase):
    """AC-10/AC-11: the Overview monitor is authenticated, bounded and honest."""

    def setUp(self):
        self.html = (WEB_DIR / 'index.html').read_text(encoding='utf-8')
        self.js = (WEB_DIR / 'app.js').read_text(encoding='utf-8')
        self.css = (WEB_DIR / 'app.css').read_text(encoding='utf-8')
        self.code = _strip_js_comments(self.js)

    def test_overview_has_monitor_hooks_and_label(self):
        for marker in (
            'id="live-state"', 'id="live-frame"', 'id="live-placeholder"',
            'id="live-toggle"', 'id="live-download"', 'id="live-detail"',
            'Local monitor',
        ):
            self.assertIn(marker, self.html, marker)
        self.assertIn('not full-rate video', self.html)
        self.assertIn('low-rate', self.html.lower())

    def test_monitor_image_is_alt_labelled_and_placeholder_is_live(self):
        self.assertIn('alt="Local camera monitor', self.html)
        self.assertIn('aria-live="polite"', self.html)
        # The state chip is announced, and the monitor card is labelled.
        self.assertIn('aria-labelledby="live-monitor-title"', self.html)

    def test_js_polls_the_authenticated_frame_and_status_endpoints(self):
        self.assertIn("fetch('/api/live/frame'", self.code)
        self.assertIn('/api/live/frame', self.code)
        self.assertIn('/api/live/status', self.code)

    def test_js_is_visibility_aware_and_bounded(self):
        self.assertIn('LIVE_INTERVAL_VISIBLE', self.code)
        self.assertIn('LIVE_INTERVAL_HIDDEN', self.code)
        self.assertIn('LIVE_MAX_BACKOFF', self.code)
        self.assertIn('document.visibilityState', self.code)

    def test_js_handles_expiry_pause_resume_and_download(self):
        self.assertIn('handleExpired()', self.code)
        self.assertIn('toggleLivePause', self.code)
        self.assertIn('downloadLiveSnapshot', self.code)
        self.assertIn("'buddy3d-snapshot.jpg'", self.code)
        self.assertIn('X-Live-State', self.code)
        self.assertIn('X-Live-Age', self.code)
        # No stale object URLs leak on repeated frames.
        self.assertIn('revokeObjectURL', self.code)

    def test_js_stops_the_monitor_when_leaving_overview_and_on_login(self):
        select = _function_body(self.code, 'selectView')
        self.assertIn('startLiveMonitor()', select)
        self.assertIn('stopLiveMonitor()', select)
        login = _function_body(self.code, 'showLogin')
        self.assertIn('stopLiveMonitor', login)

    def test_js_aborts_inflight_monitor_requests(self):
        poll = _function_body(self.code, 'pollLive')
        self.assertIn('AbortController', poll)
        self.assertIn('AbortError', poll)

    def test_css_defines_the_monitor_frame(self):
        for marker in (
            '.live-monitor__frame', '.live-monitor__image',
            '.live-monitor__placeholder',
        ):
            self.assertIn(marker, self.css, marker)


# --------------------------------------------------------------------------- #
# Packaging (AC-2)                                                             #
# --------------------------------------------------------------------------- #

class AdminAssetPackagingTests(unittest.TestCase):
    """AC-2: the factory tree and signed application bundle ship the assets."""

    def test_repo_web_tree_has_shell_and_all_allowlisted_assets(self):
        self.assertTrue((WEB_DIR / admin_http.SHELL_FILE).is_file())
        for name in admin_http.ASSET_ALLOWLIST:
            self.assertTrue((WEB_DIR / name).is_file(), name)

    def test_factory_installer_does_not_exclude_web(self):
        text = INSTALLER.read_text(encoding='utf-8')
        rsync_block = text.split('rsync -a', 1)[1].split('"$repo/pi-impersonator/"', 1)[0]
        self.assertNotIn('web', rsync_block)
        self.assertIn('"$repo/pi-impersonator/"', text)

    def test_application_release_does_not_exclude_web(self):
        text = MAKE_APP_RELEASE.read_text(encoding='utf-8')
        tar_block = text.split('tar -C "$SOURCE_DIR"', 1)[1].split('-cf - .', 1)[0]
        self.assertNotIn("exclude='web", tar_block)
        self.assertNotIn('exclude="web', tar_block)


if __name__ == '__main__':
    unittest.main()
