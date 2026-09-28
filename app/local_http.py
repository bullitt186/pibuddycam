import ipaddress
import logging
import re
from aiohttp import web
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
    app = web.Application()
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
