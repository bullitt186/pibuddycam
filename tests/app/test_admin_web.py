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


def _read_js():
    """The console's script as one text: the entry point, then every module.

    The console is split into ES modules; the source-level assertions below are
    about the behaviour of the whole script, not about which file holds a function.
    """
    names = ['app.js'] + sorted(
        path.name for path in WEB_DIR.glob('*.js') if path.name != 'app.js')
    return '\n'.join((WEB_DIR / name).read_text(encoding='utf-8') for name in names)


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

    def test_setup_shell_has_no_inline_code_and_only_allowlisted_assets(self):
        html = (WEB_DIR / admin_http.SETUP_SHELL_FILE).read_text(encoding='utf-8')
        self.assertNotIn('<style', html.lower())
        self.assertIsNone(re.search(r'<script(?![^>]*\bsrc=)', html, re.IGNORECASE))
        self.assertIsNone(re.search(r'\son[a-z]+\s*=', html, re.IGNORECASE))
        self.assertNotIn('style=', html)
        referenced = set(re.findall(r'/assets/([A-Za-z0-9._-]+)', html))
        self.assertIn('setup.js', referenced)
        self.assertLessEqual(referenced, set(admin_http.ASSET_ALLOWLIST))
        self.assertIn('Not affiliated with or endorsed by Prusa Research', html)

    def test_setup_script_uses_no_browser_storage(self):
        # The wizard state lives server-side; a captive-portal browser may
        # have no persistent storage at all.
        code = (WEB_DIR / 'setup.js').read_text(encoding='utf-8')
        for marker in ('localStorage', 'sessionStorage', 'indexedDB', 'document.cookie'):
            self.assertNotIn(marker, code, marker)

    def test_shell_references_only_allowlisted_assets(self):
        html = self.app.handle(_make_request('GET', '/admin')).body.decode('utf-8')
        referenced = set(re.findall(r'/assets/([A-Za-z0-9._-]+)', html))
        self.assertTrue(referenced)
        self.assertTrue(referenced <= set(admin_http.ASSET_ALLOWLIST))
        for name in ('app.css', 'app.js', 'favicon.svg'):
            self.assertIn(name, referenced)

    def test_every_shipped_script_is_allowlisted_and_every_import_resolves(self):
        shipped = {path.name for path in WEB_DIR.glob('*.js')}
        self.assertGreater(len(shipped), 1)
        self.assertEqual(shipped, {n for n in admin_http.ASSET_ALLOWLIST if n.endswith('.js')})
        for name in sorted(shipped):
            text = (WEB_DIR / name).read_text(encoding='utf-8')
            for target in re.findall(r"from '\./([A-Za-z0-9._-]+)\?v=__ASSET_VERSION__'", text):
                self.assertIn(target, shipped, f'{name} imports {target}')
            # only version-tagged relative imports: nothing external, nothing untagged
            for spec in re.findall(r"from '([^']+)'", text):
                self.assertRegex(spec, r'^\./[A-Za-z0-9._-]+\.js\?v=__ASSET_VERSION__$', name)

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

class AdminDesignSystemTests(unittest.TestCase):
    """AC-3: original, accessible, local-only Prusa-adjacent design tokens."""

    def setUp(self):
        self.css = (WEB_DIR / 'app.css').read_text(encoding='utf-8')
        self.html = (WEB_DIR / 'index.html').read_text(encoding='utf-8')
        self.js = _read_js()

    def test_web_tree_has_no_unknown_files(self):
        files = {path.name for path in WEB_DIR.iterdir() if path.is_file()}
        self.assertEqual(
            files, set(admin_http.ASSET_ALLOWLIST)
            | {admin_http.SHELL_FILE, admin_http.SETUP_SHELL_FILE})

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

    def test_hidden_attribute_is_authoritative_over_display_rules(self):
        # .boot/.login/.shell/.overview/.live-monitor__image set display, which
        # would otherwise override the UA [hidden] rule and show every state at
        # once (browser-found during E2E). The shell must force hidden off.
        self.assertIn('[hidden]', self.css)
        hidden_block = self.css.split('[hidden]', 1)[1].split('}', 1)[0]
        self.assertIn('display: none', hidden_block)
        self.assertIn('!important', hidden_block)

    def test_reduced_motion_is_respected(self):
        self.assertIn('prefers-reduced-motion: reduce', self.css)
        block = self.css.split('prefers-reduced-motion: reduce', 1)[1]
        self.assertIn('animation-duration', block)
        self.assertIn('transition-duration', block)

    def test_responsive_layout_covers_narrow_screens(self):
        self.assertIn('@media (max-width: 760px)', self.css)
        block = self.css.split('@media (max-width: 760px)', 1)[1]
        self.assertIn('grid-template-columns: 1fr', block)
        # The five bottom-nav labels must be able to shrink/wrap instead of
        # forcing the page grid wider than a 360px viewport.
        self.assertIn('flex-wrap: wrap', block)
        self.assertIn('min-width: 0', block)
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
        self.js = _read_js()
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
        self.js = _read_js()
        self.css = (WEB_DIR / 'app.css').read_text(encoding='utf-8')

    def test_camera_view_has_a_form_for_every_confirmed_setting(self):
        for field in (
            'camera_name', 'quality', 'rotation', 'snapshot_upload_enabled',
            'snapshot_interval', 'timelapse_enabled', 'timelapse_interval',
            'timelapse_fps', 'rtsp_mode', 'webrtc_mode',
        ):
            self.assertIn(f'data-setting="{field}"', self.html, field)

    def test_rotation_form_offers_four_angles_with_a_transpose_warning(self):
        for value in ('0', '90', '180', '270'):
            self.assertIn(f'name="rotation" value="{value}"', self.html, value)
        # The 90/270 cost warning is present but hidden until selected.
        self.assertIn('id="rotation-warning"', self.html)
        self.assertRegex(self.html, r'id="rotation-warning"[^>]*hidden')
        code = _strip_js_comments(self.js)
        self.assertIn('updateRotationWarning', code)
        self.assertIn("settings.rotation", code)
        # Rotation is sent as an int, like the coordinator's validator expects.
        self.assertIn("field === 'rotation'", code)

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

    def test_mqtt_and_prusa_forms_do_not_route_through_the_settings_patch(self):
        # #mqtt-form/#prusa-form share the .setting-form styling but have no
        # data-setting; the generic submit handler must bail so it never issues
        # a stray PATCH /api/settings.
        code = _strip_js_comments(self.js)
        body = _function_body(code, 'submitSettingForm')
        self.assertIn("form.dataset.setting", body)
        self.assertIn('if (!field) return', body)

    def test_camera_view_has_the_gpio_trigger_form_without_a_data_setting(self):
        # It writes three fields in a safe order, so it owns its submit handler and
        # must not be routed through the single-field settings handler.
        self.assertIn('id="timelapse-gpio-form"', self.html)
        form = self.html.split('id="timelapse-gpio-form"', 1)[1].split('</form>', 1)[0]
        self.assertNotIn('data-setting', form.split('>', 1)[0])
        for element in ('timelapse-trigger', 'timelapse-gpio-pin',
                        'timelapse-gpio-record-pin', 'timelapse-gpio-status',
                        'gpio-help'):
            self.assertIn(f'id="{element}"', form, element)
        code = _strip_js_comments(self.js)
        self.assertIn("'/api/gpio/pins'", code)
        self.assertIn('submitGpioForm', code)
        self.assertIn("'timelapse_gpio_record_pin'", code)

    def test_gpio_help_panel_has_the_printer_gcode_and_safety_warnings(self):
        panel = self.html.split('id="gpio-help"', 1)[1].split('</details>', 1)[0]
        for text in ('OUT0', 'M262 P0 B0', 'M264 P0 B1', 'G4 P100', 'M264 P0 B0',
                     'M264 P1 B1', 'G4 P300', 'travel_speed*60', 'Never connect a printer voltage',
                     'internal pull-up'):
            self.assertIn(text, panel, text)
        self.assertNotIn('<script', panel)

    def test_gpio_saves_are_ordered_to_never_pass_through_an_invalid_state(self):
        code = _strip_js_comments(self.js)
        body = _function_body(code, 'submitGpioForm')
        # Going to the timer: the trigger is switched first. Going to GPIO: the
        # recording pin is cleared before the layer pin moves, and the trigger is
        # switched to gpio last.
        interval_branch, gpio_branch = body.split('} else {', 1)
        self.assertIn("['timelapse_trigger', 'interval']", interval_branch)
        order = [
            gpio_branch.index("['timelapse_gpio_record_pin', null]"),
            gpio_branch.index("['timelapse_gpio_pin', shot]"),
            gpio_branch.index("['timelapse_trigger', 'gpio']"),
        ]
        self.assertEqual(order, sorted(order))
        self.assertIn('must differ', body)

    def test_network_card_exists_and_its_password_field_is_write_only(self):
        self.assertIn('id="network-card"', self.html)
        for element in ('network-ssid', 'network-psk', 'network-static',
                        'network-hostname-form', 'network-ntp-form', 'network-scan',
                        'network-result'):
            self.assertIn(f'id="{element}"', self.html, element)
        psk = self.html.split('id="network-psk"', 1)[1].split('>', 1)[0]
        self.assertIn('type="password"', psk)
        self.assertIn('autocomplete="new-password"', psk)
        self.assertNotIn('value=', psk)
        code = _strip_js_comments(self.js)
        # the stored password is never read back into the form
        self.assertIsNone(re.search(r'data\.psk(?!_set)', code))
        self.assertIn('psk_set', code)

    def test_timezone_form_uses_the_settings_path_and_the_device_zone_list(self):
        self.assertIn('id="network-timezone-form"', self.html)
        code = _strip_js_comments(self.js)
        body = _function_body(code, 'submitTimezone')
        self.assertIn("'/api/settings'", body)
        self.assertIn("field: 'timezone'", body)
        self.assertIn("'/api/timezones'", _function_body(code, 'loadTimezones'))

    def test_network_changes_reauthenticate_before_the_put(self):
        code = _strip_js_comments(self.js)
        for name, url in (('submitNetwork', '/api/network'),
                          ('submitHostname', '/api/network/hostname')):
            body = _function_body(code, name)
            self.assertIn('await requestReauth()', body, name)
            self.assertLess(body.index('await requestReauth()'),
                            body.index(f"request('{url}'"), name)
        self.assertIn('confirm: true', _function_body(code, 'submitNetwork'))

    def test_network_apply_polls_and_ignores_a_stale_result(self):
        code = _strip_js_comments(self.js)
        body = _function_body(code, 'pollNetworkApply')
        self.assertIn('netUi.baseline', body)
        self.assertIn("'reverted'", body)
        self.assertIn("'hotspot'", body)
        self.assertIn('NETWORK_POLL_MAX', body)

    def test_timelapses_view_has_the_session_picker_and_clock_warning(self):
        for element in ('timelapse-session', 'timelapse-clock-warning'):
            self.assertIn(f'id="{element}"', self.html, element)
        code = _strip_js_comments(self.js)
        self.assertIn("'/api/media/sessions'", code)
        self.assertIn("params.set('session'", code)
        self.assertIn('buildBody', code)

    def test_session_delete_is_acknowledged_and_reauthenticated_first(self):
        code = _strip_js_comments(self.js)
        body = _function_body(code, 'deleteSession')
        self.assertIn('await requestReauth()', body)
        self.assertLess(body.index('acknowledgement'), body.index('await requestReauth()'))
        self.assertLess(body.index('await requestReauth()'), body.index("method: 'DELETE'"))
        self.assertIn('confirm: true', body)
        self.assertIn('id="timelapse-session-delete-confirm"', self.html)

    def test_new_routes_are_declared_in_the_core(self):
        routes = {(route.method, route.pattern.pattern)
                  for route in admin_http.AdminApp()._routes}
        for method, pattern in (
            ('GET', r'^/api/network$'), ('PUT', r'^/api/network$'),
            ('GET', r'^/api/network/scan$'), ('PUT', r'^/api/network/hostname$'),
            ('PUT', r'^/api/network/ntp$'), ('GET', r'^/api/gpio/pins$'),
            ('GET', r'^/api/media/sessions$'),
            ('DELETE', r'^/api/media/sessions/(?P<name>[^/]+)$'),
        ):
            self.assertIn((method, pattern), routes)

    def test_js_refresh_converges_with_dashboard_settings(self):
        code = _strip_js_comments(self.js)
        self.assertIn('applySettings(data.settings)', code)
        self.assertIn('shared.dashboard', code)
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

    def test_login_reset_settles_a_pending_reauth_promise(self):
        # A session expiry while a sensitive action awaits re-auth must settle
        # the promise (as cancelled), not drop the resolver and hang forever.
        code = _strip_js_comments(self.js)
        self.assertIn('cancelPendingReauth()', _function_body(code, 'showLogin'))
        body = _function_body(code, 'cancelPendingReauth')
        self.assertIn('reauthResolver', body)
        self.assertIn('pending(false)', body)
        self.assertIn('reauthResolver = null', body)



# --------------------------------------------------------------------------- #
# Local monitor (WP-UI5; AC-10/AC-11)                                          #
# --------------------------------------------------------------------------- #

class AdminLiveMonitorUiTests(unittest.TestCase):
    """AC-10/AC-11: the Overview monitor is authenticated, bounded and honest."""

    def setUp(self):
        self.html = (WEB_DIR / 'index.html').read_text(encoding='utf-8')
        self.js = _read_js()
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
        self.assertIn("'pibuddycam-snapshot.jpg'", self.code)
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


class AdminTimelapseUiTests(unittest.TestCase):
    """WP-UI6 (AC-12/AC-13): the Timelapses view is bounded, honest, no-delete."""

    def setUp(self):
        self.html = (WEB_DIR / 'index.html').read_text(encoding='utf-8')
        self.js = _read_js()
        self.css = (WEB_DIR / 'app.css').read_text(encoding='utf-8')
        self.code = _strip_js_comments(self.js)

    def test_view_has_stats_build_filters_and_browsers(self):
        for marker in (
            'id="view-timelapses"', 'id="timelapses-state"',
            'id="timelapse-video-count"', 'id="timelapse-frame-count"',
            'id="timelapse-library-size"', 'id="timelapse-build"',
            'id="timelapse-refresh"', 'id="timelapse-build-status"',
            'id="timelapse-filters"', 'id="timelapse-videos"',
            'id="timelapse-videos-body"', 'id="timelapse-videos-empty"',
            'id="timelapse-videos-pager"', 'id="timelapse-frames-grid"',
            'id="timelapse-frames-empty"', 'id="timelapse-frames-pager"',
        ):
            self.assertIn(marker, self.html, marker)
        for status in ('all', 'completed', 'error', 'pending', 'unknown'):
            self.assertIn(f'data-timelapse-filter="{status}"', self.html)

    def test_library_offers_no_delete_for_loose_frames_and_videos_only_for_sessions(self):
        self.assertIn('SMB', self.html)
        self.assertIn('Loose frames and videos cannot be deleted here', self.html)
        for forbidden in (
            'timelapse-delete', 'media-delete', 'data-delete',
            'Delete video', 'Delete frame',
        ):
            self.assertNotIn(forbidden, self.html, forbidden)
            self.assertNotIn(forbidden, self.code, forbidden)
        # The one delete in the console targets a print session, nothing else.
        self.assertEqual(self.code.count("method: 'DELETE'"), 1)
        self.assertIn('/api/media/sessions/', self.code)
        self.assertNotIn('/api/media/timelapses/${', self.code.split("method: 'DELETE'")[0][-200:])
        self.assertNotIn('/delete', self.code)

    def test_js_lists_paginated_videos_and_frames(self):
        self.assertIn('/api/media/timelapses?', self.code)
        self.assertIn('/api/media/frames?', self.code)
        self.assertIn('TIMELAPSE_PAGE_SIZE', self.code)
        self.assertIn('TIMELAPSE_FRAME_PAGE_SIZE', self.code)
        self.assertIn('new URLSearchParams', self.code)
        self.assertIn('AbortController', self.code)

    def test_js_has_loading_empty_and_error_states(self):
        for marker in (
            'Loading the timelapse library',
            'Could not load the timelapse library',
            'No timelapse videos yet',
            'No frames stored yet',
            "setTimelapseMessage('error'",
        ):
            self.assertIn(marker, self.html + self.code, marker)

    def test_js_filters_and_pagination(self):
        self.assertIn('selectTimelapseFilter', self.code)
        self.assertIn("'aria-pressed'", self.code)
        self.assertIn('setAttribute', self.code)
        self.assertIn('changeTimelapsePage', self.code)
        self.assertIn('changeTimelapseFramePage', self.code)
        self.assertIn('renderPager', self.code)

    def test_js_build_is_csrf_guarded_and_duplicate_prevented(self):
        build = _function_body(self.code, 'buildTimelapse')
        self.assertIn("'/api/media/timelapses/build'", build)
        self.assertIn('csrf: true', build)
        self.assertIn("status === 409", build)
        self.assertIn('pollTimelapseBuild', build)
        self.assertIn('disabled = true', build)
        poll = _function_body(self.code, 'pollTimelapseBuild')
        self.assertIn('/api/media/jobs/', poll)
        self.assertIn('frames_written', poll)
        self.assertIn('TIMELAPSE_JOB_POLL_MS', poll)
        # Terminal states release the button.
        self.assertIn('disabled = false', poll)

    def test_js_build_progress_and_error_messages(self):
        for marker in ('Queuing a build', 'Building —', 'Build complete',
                       'Build failed'):
            self.assertIn(marker, self.code, marker)

    def test_js_playback_detection_and_download_fallback(self):
        self.assertIn('canPlayType', self.code)
        self.assertIn('video/x-msvideo', self.code)
        self.assertIn('cannot play MJPEG AVI inline', self.code)
        self.assertIn('setAttribute(\'download\'', self.code)

    def test_js_loads_on_view_and_stops_on_login(self):
        select = _function_body(self.code, 'selectView')
        self.assertIn('loadTimelapses()', select)
        login = _function_body(self.code, 'showLogin')
        self.assertIn('stopTimelapsePolling', login)

    def test_css_defines_timelapse_components(self):
        for marker in (
            '.filters', '.media-table', '.media-grid', '.media-grid__item',
            '.pager', '.pager__label', '.overview-state--loading',
        ):
            self.assertIn(marker, self.css, marker)

    def test_video_table_scrolls_instead_of_overflowing_at_360px(self):
        # Six columns cannot fit 360px; the table must scroll inside its own
        # box so the page itself never gains a horizontal scrollbar.
        table = self.html.index('id="timelapse-videos"')
        wrapper = self.html.rindex('table-scroll', 0, table)
        self.assertLess(wrapper, table)
        self.assertIn('.table-scroll', self.css)
        scroll_block = self.css.split('.table-scroll', 1)[1].split('}', 1)[0]
        self.assertIn('overflow-x: auto', scroll_block)


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
        rsync_block = text.split('rsync -a', 1)[1].split('"$repo/app/"', 1)[0]
        self.assertNotIn('web', rsync_block)
        self.assertIn('"$repo/app/"', text)

    def test_application_release_does_not_exclude_web(self):
        text = MAKE_APP_RELEASE.read_text(encoding='utf-8')
        tar_block = text.split('tar -C "$SOURCE_DIR"', 1)[1].split('-cf - .', 1)[0]
        self.assertNotIn("exclude='web", tar_block)
        self.assertNotIn('exclude="web', tar_block)


# --------------------------------------------------------------------------- #
# System view (WP-UI7; AC-14..AC-17)                                           #
# --------------------------------------------------------------------------- #

class AdminSystemUiTests(unittest.TestCase):
    """The System view is read-only on load and gates every destructive action."""

    def setUp(self):
        self.html = (WEB_DIR / 'index.html').read_text(encoding='utf-8')
        self.js = _strip_js_comments(_read_js())
        self.css = (WEB_DIR / 'app.css').read_text(encoding='utf-8')

    def test_system_placeholder_is_replaced_with_real_sections(self):
        self.assertNotIn('System information is not available in this release', self.html)
        for marker in (
            'system-health-title', 'system-update-title', 'system-diagnostics-title',
            'system-access-title', 'system-danger-title',
            'system-app-version', 'system-release', 'system-commit',
            'system-provisioning', 'system-ssh-enabled', 'system-ssh-save',
            'system-recovery', 'system-reboot', 'system-reset',
        ):
            self.assertIn(marker, self.html, marker)

    def test_update_controls_and_warning(self):
        for marker in (
            'system-update-check', 'system-update-install',
            'system-update-installed', 'system-update-latest',
            'system-update-checked', 'system-update-chip',
        ):
            self.assertIn(marker, self.html, marker)
        self.assertIn('report-only', self.html)
        self.assertIn('may disconnect', self.html)

    def test_diagnostics_controls(self):
        for marker in (
            'system-diagnostics-load', 'system-diagnostics-download',
            'system-diagnostics-output',
        ):
            self.assertIn(marker, self.html, marker)
        self.assertIn('No command, unit, or path can be supplied', self.html)

    def test_danger_zone_has_typed_reset_and_include_media(self):
        self.assertIn('system-reset-phrase', self.html)
        self.assertIn('Type <strong>RESET</strong>', self.html)
        self.assertIn('system-reset-media', self.html)
        self.assertIn('system-reboot-confirm', self.html)

    def test_load_only_runs_read_only_gets(self):
        body = _function_body(self.js, 'loadSystem')
        self.assertIn('/api/system', body)
        self.assertIn('/api/update', body)
        for forbidden in ('/api/reboot', '/api/reset/', '/api/update/install',
                          '/api/update/check', '/api/ssh', '/api/recovery'):
            self.assertNotIn(forbidden, body)

    def test_selecting_system_loads_it(self):
        body = _function_body(self.js, 'selectView')
        self.assertIn("name === 'system'", body)
        self.assertIn('loadSystem()', body)

    def test_sensitive_actions_reauthenticate_first(self):
        for name in ('installUpdate', 'saveSsh', 'enterRecovery',
                     'rebootDevice', 'factoryReset'):
            body = _function_body(self.js, name)
            self.assertIn('requestReauth', body, name)
            reauth_at = body.index('requestReauth')
            self.assertLess(
                reauth_at, body.find('request('),
                f'{name} must re-authenticate before its request',
            )

    def test_exact_endpoint_schemas(self):
        ssh = _function_body(self.js, 'saveSsh')
        self.assertIn("'/api/ssh'", ssh)
        self.assertIn('{ enabled }', ssh)
        reboot = _function_body(self.js, 'rebootDevice')
        self.assertIn("'/api/reboot'", reboot)
        self.assertIn('{ confirm: true }', reboot)
        reset = _function_body(self.js, 'factoryReset')
        self.assertIn("'/api/reset/begin'", reset)
        self.assertIn("'/api/reset/confirm'", reset)
        self.assertIn("'/api/reset/execute'", reset)
        self.assertIn('{ include_timelapse: includeMedia }', reset)
        recovery = _function_body(self.js, 'enterRecovery')
        self.assertIn("'/api/recovery/enter-setup'", recovery)
        check = _function_body(self.js, 'checkForUpdate')
        self.assertIn("'/api/update/check'", check)
        install = _function_body(self.js, 'installUpdate')
        self.assertIn("'/api/update/install'", install)

    def test_reset_requires_the_typed_phrase(self):
        body = _function_body(self.js, 'factoryReset')
        self.assertIn("'RESET'", body)
        ready = _function_body(self.js, 'resetPhraseReady')
        self.assertIn('RESET', ready)
        self.assertIn('disabled', ready)

    def test_reconnect_uncertainty_is_reported(self):
        install = _function_body(self.js, 'installUpdate')
        self.assertIn('lost connection', install)
        self.assertIn('status === 0', install)
        reboot = _function_body(self.js, 'rebootDevice')
        self.assertIn('status === 0', reboot)

    def test_no_destructive_action_is_wired_to_a_form_submit(self):
        # The System actions are button click handlers, never form submits.
        wire = _function_body(self.js, 'wireForms')
        for marker in ('systemReboot', 'systemReset', 'systemUpdateInstall',
                       'systemSshSave'):
            self.assertIn(f'els.{marker}', wire)

    def test_css_defines_danger_and_diagnostics_styles(self):
        for marker in ('.btn--danger', '.card--danger', '.danger-action',
                       '.diagnostics'):
            self.assertIn(marker, self.css, marker)


if __name__ == '__main__':
    unittest.main()
