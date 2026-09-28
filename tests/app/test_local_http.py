import asyncio
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

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
