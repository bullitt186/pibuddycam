import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

try:
    from aiohttp import web  # noqa: E402
    from aiohttp.test_utils import make_mocked_request  # noqa: E402
except ImportError:  # the host test environment does not always provide aiohttp
    raise unittest.SkipTest('aiohttp is not installed')

import local_http  # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def get(path, host):
    req = make_mocked_request('GET', path, headers={'Host': host})
    try:
        return run(local_http.handle_root(req))
    except web.HTTPFound as found:
        return found


class RedirectTests(unittest.TestCase):
    def test_root_and_admin_redirect_for_ip_host(self):
        for path in ('/', '/admin'):
            resp = get(path, '192.0.2.10')
            self.assertEqual(resp.status, 302)
            self.assertEqual(resp.headers['Location'], 'https://192.0.2.10/admin')

    def test_local_hostname_redirects(self):
        resp = get('/', 'pibuddycam.local')
        self.assertEqual(resp.headers['Location'], 'https://pibuddycam.local/admin')

    def test_port_is_stripped(self):
        resp = get('/', '192.0.2.10:80')
        self.assertEqual(resp.headers['Location'], 'https://192.0.2.10/admin')

    def test_hostile_host_gets_plain_text(self):
        for host in ('evil.example/x@y', 'a b', 'ex\\ample', '[::1]', '.', '-a'):
            resp = get('/', host)
            self.assertNotEqual(resp.status, 302, host)
            self.assertEqual(resp.text, 'PiBuddyCam')


PORTAL = '192.168.4.1'
PROBES = (
    ('captive.apple.com', '/hotspot-detect.html'),
    ('connectivitycheck.gstatic.com', '/generate_204'),
    ('www.msftconnecttest.com', '/connecttest.txt'),
    ('detectportal.firefox.com', '/canonical.html'),
)


def transport_on(address):
    transport = mock.Mock()
    transport.get_extra_info.side_effect = (
        lambda name, default=None: (address, 80) if name == 'sockname' else default)
    return transport


def portal(path, host, local=PORTAL, method='GET', transport=None):
    headers = {} if host is None else {'Host': host}
    req = make_mocked_request(
        method, path, headers=headers,
        transport=transport if transport is not None else transport_on(local))
    return local_http._hotspot_portal_response(req)


class HotspotPortalTests(unittest.TestCase):
    """Probes on the setup hotspot of a claimed device get the info page."""

    def setUp(self):
        patcher = mock.patch.object(local_http.socket, 'gethostname', return_value='pibuddycam')
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_page(self, resp, label):
        self.assertIsNotNone(resp, label)
        self.assertEqual(resp.status, 200, label)
        self.assertEqual(resp.content_type, 'text/html', label)
        self.assertEqual(resp.headers['Cache-Control'], 'no-store', label)
        self.assertNotIn('Location', resp.headers, label)
        self.assertIn('https://192.168.4.1/admin', resp.text, label)

    def test_portal_address_is_the_hotspot_one(self):
        import hotspot
        self.assertEqual(hotspot.CAPTIVE_PORTAL_IP, PORTAL)

    def test_connectivity_probes_get_the_info_page(self):
        for host, path in PROBES:
            self.assert_page(portal(path, host), host)

    def test_head_probe_gets_the_info_page(self):
        self.assert_page(portal('/generate_204', 'connectivitycheck.gstatic.com', method='HEAD'),
                         'HEAD')

    def test_announced_setup_uri_gets_the_info_page(self):
        # DHCP option 114 announces http://192.168.4.1/setup.
        for path in ('/setup', '/setup/state'):
            self.assert_page(portal(path, PORTAL), path)

    def test_own_host_is_routed_normally(self):
        for host in (PORTAL, PORTAL + ':80', 'pibuddycam', 'PiBuddyCam.local', 'localhost'):
            for path in ('/', '/admin', '/snapshot.jpg', '/nope'):
                self.assertIsNone(portal(path, host), (host, path))

    def test_lan_is_untouched(self):
        for host, path in PROBES:
            self.assertIsNone(portal(path, host, local='192.0.2.10'), host)
        self.assertIsNone(portal('/setup', '192.0.2.10', local='192.0.2.10'))

    def test_only_get_and_head(self):
        for method in ('POST', 'PUT', 'DELETE'):
            self.assertIsNone(portal('/onvif/device_service', 'captive.apple.com', method=method),
                              method)

    def test_missing_host_or_socket_is_left_alone(self):
        self.assertIsNone(portal('/generate_204', None))
        self.assertIsNone(portal('/generate_204', '   '))
        no_sockname = mock.Mock()
        no_sockname.get_extra_info.return_value = None
        self.assertIsNone(portal('/generate_204', 'captive.apple.com', transport=no_sockname))

    def test_hostile_host_is_never_echoed(self):
        resp = portal('/x', 'evil.example/<script>alert(1)</script>')
        self.assert_page(resp, 'hostile')
        self.assertNotIn('evil', resp.text)
        self.assertNotIn('<script>', resp.text)


class RoutedAppTests(unittest.IsolatedAsyncioTestCase):
    """The real routes: the redirect must not disturb snapshot or ONVIF."""

    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer
        self._old_jpeg = local_http.last_jpeg
        local_http.last_jpeg = b'\xff\xd8\xff-fake-jpeg'
        self.client = TestClient(TestServer(local_http.build_local_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        local_http.last_jpeg = self._old_jpeg

    async def test_root_and_admin_answer_302_and_do_not_follow(self):
        for path in ('/', '/admin'):
            response = await self.client.get(
                path, headers={'Host': '192.0.2.10'}, allow_redirects=False)
            self.assertEqual(response.status, 302, path)
            self.assertEqual(response.headers['Location'], 'https://192.0.2.10/admin')

    async def test_hostile_host_is_served_the_old_plain_text_page(self):
        response = await self.client.get(
            '/', headers={'Host': 'evil.example/x'}, allow_redirects=False)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.text(), 'PiBuddyCam')

    async def test_snapshot_is_untouched(self):
        response = await self.client.get('/snapshot.jpg', allow_redirects=False)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Content-Type'], 'image/jpeg')
        self.assertEqual(await response.read(), b'\xff\xd8\xff-fake-jpeg')

    async def test_onvif_routes_exist_only_with_a_context_and_are_untouched(self):
        response = await self.client.post('/onvif/device_service', data=b'x')
        self.assertEqual(response.status, 404)      # no ONVIF context: not registered
        import onvif_facade
        from state import CameraState
        context = onvif_facade.OnvifContext.create(
            CameraState(), '192.0.2.10', 'aa-bb-cc-dd-ee-ff', '3.1.6', stable_seed='s')
        from aiohttp.test_utils import TestClient, TestServer
        client = TestClient(TestServer(local_http.build_local_app(context)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        response = await client.post(
            '/onvif/device_service', data=b'<not-soap/>',
            headers={'Content-Type': 'application/soap+xml'})
        self.assertNotEqual(response.status, 302)
        self.assertNotEqual(response.status, 404)

    async def test_unknown_path_with_foreign_host_stays_404_on_the_lan(self):
        response = await self.client.get(
            '/generate_204', headers={'Host': 'connectivitycheck.gstatic.com'},
            allow_redirects=False)
        self.assertEqual(response.status, 404)

    async def test_hotspot_probe_reaches_the_info_page_through_the_app(self):
        # The test server listens on 127.0.0.1; pretend that is the hotspot.
        with mock.patch.object(local_http, 'CAPTIVE_PORTAL_IP', '127.0.0.1'):
            response = await self.client.get(
                '/generate_204', headers={'Host': 'connectivitycheck.gstatic.com'},
                allow_redirects=False)
            self.assertEqual(response.status, 200)
            self.assertIn('/admin', await response.text())
            response = await self.client.get(
                '/snapshot.jpg', headers={'Host': '127.0.0.1'}, allow_redirects=False)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['Content-Type'], 'image/jpeg')

    async def test_the_updater_health_check_is_a_plain_tcp_connect(self):
        # The signed-update health check only opens a TCP connection to :80; a
        # redirect response does not change that.
        server = self.client.server
        import asyncio
        reader, writer = await asyncio.open_connection(server.host, server.port)
        writer.close()
        await writer.wait_closed()


if __name__ == '__main__':
    unittest.main()
