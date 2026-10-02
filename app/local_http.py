import ipaddress
import logging
import re
import socket
from aiohttp import web
from hotspot import CAPTIVE_PORTAL_IP
from onvif_facade import soap_response

log = logging.getLogger('pibuddycam.http')

# Populated by main.py's snapshot loop; served instantly to avoid slow on-demand capture.
last_jpeg: bytes = b''

async def handle_snapshot(request):
    jpeg = last_jpeg
    if not jpeg:
        try:
            from camera import capture_jpeg
            from quality import read_current
            # Review fix 5: one state drives snapshots — use the shared quality
            # resolution instead of a hardcoded 1920x1080.
            _, width, height = read_current()
            jpeg = capture_jpeg(width, height)
        except Exception as e:
            log.error(f'local /snapshot.jpg cold-start capture: {e}')
            return web.Response(status=503, text='Camera unavailable')
    return web.Response(body=jpeg, content_type='image/jpeg')

_HOST_NAME = re.compile(r'^[A-Za-z0-9.-]{1,253}$')


def _redirect_host(request):
    """Return the Host header without its port if it is safe to echo, else None.

    Only an IPv4 address or a plain hostname is accepted, so a hostile Host
    header can never turn the redirect into an open redirect.
    """
    raw = (request.host or '').strip()
    if not raw or raw.startswith('['):
        return None
    host = raw.rsplit(':', 1)[0] if ':' in raw else raw
    try:
        ipaddress.IPv4Address(host)
        return host
    except ValueError:
        pass
    if _HOST_NAME.match(host) and not host.startswith(('.', '-')):
        return host
    return None


def _host_name(raw):
    """Return the lower-cased host part of a ``Host`` header (port stripped)."""
    raw = (raw or '').strip().lower()
    if raw.startswith('['):
        return raw.split(']', 1)[0] + ']'
    return raw.rsplit(':', 1)[0] if ':' in raw else raw


def _own_host_names():
    """The names this device answers to on the setup hotspot."""
    names = {CAPTIVE_PORTAL_IP, 'localhost', '127.0.0.1'}
    try:
        hostname = socket.gethostname().strip().lower()
    except OSError:
        hostname = ''
    if hostname:
        names.update({hostname, hostname + '.local'})
    return names


def _local_address(request):
    """Return the local IP the request arrived on, or None."""
    transport = request.transport
    if transport is None:
        return None
    try:
        sockname = transport.get_extra_info('sockname')
    except Exception:
        return None
    if not sockname:
        return None
    return sockname[0]


# Served on the setup hotspot instead of a 404. Static on purpose: nothing from
# the request is echoed, so it cannot be turned into an injection or a redirect.
_HOTSPOT_PAGE = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<title>PiBuddyCam</title></head>'
    '<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:2rem auto;'
    'padding:0 1rem;line-height:1.5">'
    '<h1>PiBuddyCam</h1>'
    '<p>The camera could not reach its Wi-Fi network and started this setup network.</p>'
    '<p>Open <a href="https://' + CAPTIVE_PORTAL_IP + '/admin">https://'
    + CAPTIVE_PORTAL_IP + '/admin</a> in your browser, accept the certificate warning, '
    'sign in and fix the network under System, Network.</p>'
    '<p>The camera keeps retrying its old network and stops this setup network once it '
    'is back.</p>'
    '</body></html>'
)


def _hotspot_portal_response(request):
    """Answer a captive-portal probe on the setup hotspot, else return None.

    On a claimed device the network watchdog or a failed network change can
    start the setup hotspot while this server owns port 80. The hotspot's DNS
    resolves every name to the portal address, so the operating systems'
    connectivity probes land here. A 404 would show up as "404: Not Found" in
    the phone's sign-in window; this static page tells the user where the
    console is instead. ``/setup`` gets it too, for anyone who remembers the
    setup address from the first setup. It is not a redirect to HTTPS because
    the captive sign-in windows cannot get past the self-signed certificate.

    Only ``GET``/``HEAD`` requests that arrived on the hotspot address count,
    with a foreign ``Host`` or for ``/setup``. Everything else, in particular
    every request on the normal LAN, is routed as before.
    """
    if request.method not in ('GET', 'HEAD'):
        return None
    if _local_address(request) != CAPTIVE_PORTAL_IP:
        return None
    host = request.headers.get('Host')
    if not host or not host.strip():
        return None
    path = request.path
    if _host_name(host) in _own_host_names() and not (
            path == '/setup' or path.startswith('/setup/')):
        return None
    return web.Response(
        text=_HOTSPOT_PAGE, content_type='text/html', charset='utf-8',
        headers={'Cache-Control': 'no-store'})


@web.middleware
async def _hotspot_portal_middleware(request, handler):
    response = _hotspot_portal_response(request)
    if response is not None:
        return response
    return await handler(request)


async def handle_root(request):
    host = _redirect_host(request)
    if host is None:
        return web.Response(text='PiBuddyCam', content_type='text/plain')
    raise web.HTTPFound(f'https://{host}/admin')

async def handle_onvif(request):
    context = request.app['onvif_context']
    service = request.match_info['service']
    payload = await request.read()
    status, body = soap_response(service, context, payload)
    return web.Response(
        status=status,
        body=body,
        content_type='application/soap+xml',
        charset='utf-8',
    )


def build_local_app(onvif_context=None):
    """Build the port-80 application (a separate step so tests can drive it)."""
    app = web.Application(middlewares=[_hotspot_portal_middleware])
    app.router.add_get('/', handle_root)
    app.router.add_get('/admin', handle_root)
    app.router.add_get('/snapshot.jpg', handle_snapshot)
    if onvif_context is not None:
        app['onvif_context'] = onvif_context
        app.router.add_post('/onvif/{service:device|media}_service', handle_onvif)
    return app


async def start_local_http(onvif_context=None):
    app = build_local_app(onvif_context)
    runner = web.AppRunner(app, access_log=log)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 80)
    await site.start()
    log.info('Local HTTP/ONVIF server started on port 80')
    return runner
