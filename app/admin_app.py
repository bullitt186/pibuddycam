"""Thin aiohttp transport for the admin/provisioning UI (WP-3e2, AC-17/AC-19/AC-20).

This is the *only* place the admin surface touches a real socket. It is a thin
adapter over the already-accepted stdlib core in :mod:`admin_http`: it builds an
``aiohttp`` application whose routes map 1:1 onto :class:`admin_http.AdminApp`
routes, converts an incoming request into an :class:`admin_http.Request`, calls
:meth:`admin_http.AdminApp.handle`, and writes the returned
:class:`admin_http.Response` back.

Peer identity
-------------
``peer_ip`` is taken from the TCP transport's socket peer
(``request.transport.get_extra_info('peername')``, falling back to
``request.remote``). Forwarding headers are deliberately ignored: ``X-Forwarded-For``
and ``X-Real-IP`` are never read, so a client cannot spoof its address to evade
the login/re-auth rate limiter or the trusted-LAN labelling. This appliance is
not deployed behind a reverse proxy.

Bind port by mode
-----------------
The pre-claim captive portal binds TCP ``80`` in ``setup`` mode
(``http://192.168.4.1``); the post-claim administration UI binds TCP ``443`` in
``admin`` mode and is reached at ``https://pibuddycam-<device-id>.local`` with a
device-generated self-signed certificate (a browser warning is acceptable and
documented in the source plan §4.5). The bind host/port and TLS context are all
injectable.

TLS is **mandatory in admin mode**: the boot-time provisioner
(:mod:`admin_tls`, run by ``pi-persist.service``) generates the durable keypair
and recreates ``/etc/pibuddycam/admin.env``. When that configuration is missing
or invalid, ``run`` raises and the service fails instead of serving the console
as plaintext. Setup mode stays plain HTTP on the captive portal.

Secret hygiene
--------------
This module logs no request body and installs no aiohttp access log (which would
print query strings); the core redacts every response body and every logged
header mapping. Configuration is read through the existing modules
(:mod:`provisioning`, :mod:`privileged`); no secret value is read here.

Stdlib-only tests parse this file with :mod:`ast` and never import it, so the
top-level ``aiohttp`` import is intentional and expected.
"""
import argparse
import asyncio
import concurrent.futures
import json
import logging
import os
from urllib.parse import urlsplit

from aiohttp import web

import admin_auth
import admin_http
import admin_tls
import app_version
import camera_probe
import dashboard
import diagnostics
import image_guard
import live_monitor
import local_webrtc_signaling
import media_build
import mqtt_probe
import network_settings
import privileged
import provisioning
import runtime_ipc
import update_control

log = logging.getLogger('pibuddycam.admin_app')

#: Default broker-test callable for the wizard/admin MQTT route (WP-R2).
#: Resolved at import so the ``build_admin_app`` parameter can also be named
#: ``mqtt_probe`` without shadowing the module.
_DEFAULT_MQTT_PROBE = mqtt_probe.probe

#: The captive-portal bind port while the device is unclaimed (setup mode).
DEFAULT_SETUP_PORT = 80

#: The post-claim administration bind port (admin mode, HTTPS).
DEFAULT_ADMIN_PORT = 443

#: Default bind host. The plan requires the unclaimed wizard to stay off the
#: normal LAN interface; the transport is bound to a specific address only when
#: the caller (setup hotspot / systemd) injects one.
DEFAULT_BIND_HOST = '0.0.0.0'

#: Valid application modes.
VALID_MODES = ('setup', 'admin')

#: aiohttp route table: ``(method, path)`` pairs that map 1:1 onto
#: :class:`admin_http.AdminApp` routes (same method and equivalent path shape).
#: ``/setup/step/{n}`` is the aiohttp form of ``admin_http``'s
#: ``^/setup/step/(?P<n>[^/]+)$``. Keep this in lock-step with
#: :meth:`admin_http.AdminApp._build_routes`.
ROUTES = (
    ('GET', '/'),
    ('GET', '/admin'),
    ('GET', '/assets/{name}'),
    ('GET', '/setup'),
    ('POST', '/setup/step/{n}'),
    ('POST', '/setup/finish'),
    ('GET', '/api/status'),
    ('GET', '/api/session'),
    ('GET', '/api/dashboard'),
    ('GET', '/api/live/frame'),
    ('GET', '/api/live/status'),
    ('GET', '/api/live/webrtc/status'),
    ('GET', '/api/media/timelapses'),
    ('GET', '/api/media/timelapses/{name}'),
    ('GET', '/api/media/frames'),
    ('GET', '/api/media/frames/{name}'),
    ('GET', '/api/media/sessions'),
    ('POST', '/api/media/timelapses/build'),
    ('GET', '/api/media/jobs/{id}'),
    ('GET', '/api/system'),
    ('GET', '/api/network'),
    ('GET', '/api/network/scan'),
    ('PUT', '/api/network'),
    ('PUT', '/api/network/hostname'),
    ('PUT', '/api/network/ntp'),
    ('GET', '/api/gpio/pins'),
    ('GET', '/api/update'),
    ('POST', '/api/update/check'),
    ('POST', '/api/update/install'),
    ('GET', '/api/diagnostics'),
    ('POST', '/api/reboot'),
    ('PATCH', '/api/settings'),
    ('GET', '/api/integrations'),
    ('PUT', '/api/integrations/mqtt'),
    ('PUT', '/api/integrations/prusa'),
    ('POST', '/api/login'),
    ('POST', '/api/mqtt/test'),
    ('POST', '/api/logout'),
    ('POST', '/api/reauth'),
    ('GET', '/api/config'),
    ('PUT', '/api/config'),
    ('POST', '/api/ssh'),
    ('POST', '/api/recovery/enter-setup'),
    ('POST', '/api/reset/begin'),
    ('POST', '/api/reset/confirm'),
    ('POST', '/api/reset/execute'),
)

#: Provisioning states at/after which the admin UI (not the wizard) is served.
_CLAIMED_STATES = frozenset({'claimed', 'configured', 'running'})


# --------------------------------------------------------------------------- #
# Request / response translation
# --------------------------------------------------------------------------- #

def _peer_ip(request):
    """Return the socket peer address, never a forwarding header.

    The transport's ``peername`` is authoritative. ``request.remote`` is only a
    fallback for a transport without a peer name; it is still aiohttp's own
    socket-derived value, not a client header.
    """
    transport = getattr(request, 'transport', None)
    if transport is not None:
        try:
            peername = transport.get_extra_info('peername')
        except Exception:  # noqa: BLE001 - a closed transport must not crash a request
            peername = None
        if isinstance(peername, (tuple, list)) and peername:
            return str(peername[0])
        if isinstance(peername, str) and peername:
            return peername
    remote = getattr(request, 'remote', None)
    return remote if isinstance(remote, str) else ''


def _query_dict(request):
    """Return the parsed query string as a plain ``dict``."""
    try:
        return dict(request.query)
    except Exception:  # noqa: BLE001 - a malformed query must not crash a request
        return {}


def _header_dict(request):
    """Return the request headers as a plain ``dict`` (core does its own lookup)."""
    try:
        return dict(request.headers)
    except Exception:  # noqa: BLE001
        return {}


def _to_request(request, body):
    """Translate an aiohttp request into an :class:`admin_http.Request`."""
    return admin_http.Request(
        method=request.method,
        path=request.path,
        query=_query_dict(request),
        headers=_header_dict(request),
        body=body,
        peer_ip=_peer_ip(request),
    )


def _to_response(response: admin_http.Response) -> web.Response:
    """Translate an :class:`admin_http.Response` into an aiohttp response.

    ``Set-Cookie`` (and every other core header) is passed through verbatim, so
    the session cookie minted by the core is preserved unchanged.
    """
    return web.Response(
        status=response.status,
        headers=dict(response.headers),
        body=response.body,
    )


#: Single-worker executor for the stdlib admin core. ``AdminApp`` is not
#: thread-safe (no locks around its session store or rate limiter), so exactly
#: one worker guarantees the core's previous single-threaded semantics while
#: keeping blocking work off the aiohttp event loop. Without this, the MQTT
#: broker-test route (blocking DNS/TCP/TLS/paho waits) would freeze the whole
#: provisioning/admin UI.
_CORE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix='admin-core')

#: Bounded chunk size for streaming a core ``Response.file`` (WP-UI6/AC-12).
#: The core has already authorized, allowlisted, ``fstat``-revalidated, and
#: positioned the descriptor, so the transport only copies a bounded chunk at a
#: time and never buffers a whole AVI.
_STREAM_CHUNK = 64 * 1024

#: Dedicated executor for file reads. Kept separate from the single admin-core
#: worker so a long download cannot serialize unrelated API requests.
_STREAM_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix='admin-stream')

#: Dedicated executor for local-WebRTC pipeline control calls (start/stop/
#: handle_answer/add_ice_candidate). Kept separate from ``_CORE_EXECUTOR`` so a
#: GStreamer call (``handle_answer``'s ``promise.wait()`` in particular) can
#: never serialize behind, or be serialized behind, unrelated admin API
#: requests.
_LOCAL_WEBRTC_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix='admin-webrtc')

#: Env var gating the local-WebRTC live-video signaling route. Default on; set
#: to ``0``/``false``/``no``/``off`` to disable (e.g. a resource-constrained
#: deployment). When disabled, ``/api/live/webrtc`` is never registered and
#: GStreamer/PyGObject are never imported by the admin process.
_LOCAL_WEBRTC_ENABLED_ENV = 'ADMIN_LOCAL_WEBRTC_ENABLED'


def _local_webrtc_enabled(env=None):
    """Return whether the local-WebRTC signaling route should be registered."""
    env = os.environ if env is None else env
    raw = (env.get(_LOCAL_WEBRTC_ENABLED_ENV) or '1').strip().lower()
    return raw not in ('0', 'false', 'no', 'off')


async def _stream_response(request, core_response):
    """Stream one core file response in bounded chunks and always close it.

    Authentication and allowlisting happen in the core before the descriptor is
    handed over, so this adapter never duplicates auth. The copy is bounded by
    the core-computed ``file_length`` (so it can never overread a grown file),
    and the descriptor is closed in ``finally`` on success, disconnect, or any
    error -- including a failure while constructing/preparing the response.
    """
    handle = core_response.file
    try:
        headers = dict(core_response.headers)
        length = int(getattr(core_response, 'file_length', 0) or 0)
        headers.setdefault('Content-Length', str(length))
        stream = web.StreamResponse(status=core_response.status, headers=headers)
        loop = asyncio.get_running_loop()
        await stream.prepare(request)
        remaining = length
        while remaining > 0:
            chunk = await loop.run_in_executor(
                _STREAM_EXECUTOR, handle.read, min(_STREAM_CHUNK, remaining))
            if not chunk:
                break
            await stream.write(chunk)
            remaining -= len(chunk)
        await stream.write_eof()
        return stream
    finally:
        try:
            handle.close()
        except Exception:  # noqa: BLE001 - closing must never mask the response
            pass


async def _handle(request):
    """Dispatch one aiohttp request through the stdlib admin core.

    The core runs on the dedicated single worker thread, never inline on the
    event loop, so a blocking handler (the MQTT broker probe) cannot stall
    other requests or the loop. A media response carrying an open descriptor is
    streamed by the transport.
    """
    app = request.app['admin_app']
    body = await request.read()
    core_request = _to_request(request, body)
    loop = asyncio.get_running_loop()
    core_response = await loop.run_in_executor(
        _CORE_EXECUTOR, app.handle, core_request)
    if getattr(core_response, 'file', None) is not None:
        return await _stream_response(request, core_response)
    return _to_response(core_response)


# --------------------------------------------------------------------------- #
# Local-WebRTC live-video signaling (bypasses the stdlib core; see
# local_webrtc_signaling's module docstring for why a WS upgrade cannot go
# through AdminApp.handle())
# --------------------------------------------------------------------------- #

def _origin_allowed(request):
    """True only when the WS handshake's ``Origin`` matches this request's host.

    A real browser always sends ``Origin`` on a WebSocket handshake. The
    session cookie is already ``SameSite=Lax`` (admin_auth.py), which blocks a
    cross-site page's JS from having it attached to a WS open in the first
    place; this Origin check is belt-and-braces on top of that, not the
    primary defense.
    """
    origin = request.headers.get('Origin')
    if not origin:
        return False
    try:
        return urlsplit(origin).netloc == request.host
    except ValueError:
        return False


async def _handle_local_webrtc_ws(request):
    """Local-only WebRTC signaling WebSocket for the console's live-video view.

    Does its own session-cookie authentication (reusing the same
    ``admin_auth.SessionStore`` the core uses, through
    ``AdminApp.validate_session_token``) and its own Origin check, then hands
    the connection to a fresh ``local_webrtc.LocalWebRTC`` pipeline for the
    life of the socket. One viewer slot is held from ``try_acquire()`` until
    the ``finally`` block's ``release()``, whatever the exit path.
    """
    core = request.app['admin_app']
    registry = core.local_webrtc_viewers

    if not _origin_allowed(request):
        return web.Response(status=403, text='origin not allowed')

    token = request.cookies.get(admin_auth.SESSION_COOKIE_NAME, '')
    if not token or not core.validate_session_token(token):
        return web.Response(status=401, text='authentication required')

    if registry is None or not registry.try_acquire():
        ws = web.WebSocketResponse(heartbeat=15.0)
        await ws.prepare(request)
        await ws.send_str(json.dumps({'type': 'error', 'code': 'viewer_limit'}))
        await ws.close(code=1008, message=b'viewer limit reached')
        return ws

    ws = web.WebSocketResponse(heartbeat=15.0)
    await ws.prepare(request)
    loop = asyncio.get_running_loop()

    async def send(message):
        if not ws.closed:
            await ws.send_str(json.dumps(message))

    async def on_offer(sdp_text):
        await send({'type': 'offer', 'sdp': sdp_text})

    async def on_ice_candidate(candidate, mline_index):
        await send({'type': 'ice', 'candidate': candidate, 'sdpMLineIndex': mline_index})

    async def on_ended(reason):
        await send({'type': 'ended', 'reason': reason})
        if not ws.closed:
            await ws.close()

    # Lazy: GStreamer/PyGObject are only imported once a viewer is actually
    # accepted, mirroring live_monitor.default_producer's lazy camera import.
    import local_webrtc

    session = local_webrtc.LocalWebRTC(on_offer, on_ice_candidate, on_ended, loop)
    try:
        await loop.run_in_executor(_LOCAL_WEBRTC_EXECUTOR, session.start)
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    msg_type, payload = local_webrtc_signaling.parse_client_message(msg.data)
                except local_webrtc_signaling.SignalingError as exc:
                    await send({'type': 'error', 'code': 'bad_message', 'message': str(exc)})
                    continue
                if msg_type == 'answer':
                    await loop.run_in_executor(
                        _LOCAL_WEBRTC_EXECUTOR, session.handle_answer, payload['sdp'])
                elif msg_type == 'ice':
                    await loop.run_in_executor(
                        _LOCAL_WEBRTC_EXECUTOR, session.add_ice_candidate,
                        payload['candidate'], payload['sdpMLineIndex'])
                elif msg_type == 'stop':
                    break
            elif msg.type in (
                web.WSMsgType.ERROR, web.WSMsgType.CLOSE, web.WSMsgType.CLOSING,
            ):
                break
    finally:
        await loop.run_in_executor(_LOCAL_WEBRTC_EXECUTOR, session.stop)
        registry.release()
    return ws


# --------------------------------------------------------------------------- #
# Application factory
# --------------------------------------------------------------------------- #

async def _cleanup(app):
    """Stop the shared live-monitor producer on transport shutdown.

    ``AdminApp.close`` may join the producer thread, so it runs on the core
    executor rather than the event loop. Never raises.
    """
    core = app.get('admin_app')
    if core is None:
        return
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(_CORE_EXECUTOR, core.close)
    except Exception:  # noqa: BLE001 - shutdown must never raise
        log.debug('admin_app: live monitor cleanup failed', exc_info=True)


def create_app(admin_app: admin_http.AdminApp) -> web.Application:
    """Build the aiohttp application bound to an :class:`admin_http.AdminApp`."""
    app = web.Application()
    app['admin_app'] = admin_app
    app.on_cleanup.append(_cleanup)
    for method, path in ROUTES:
        app.router.add_route(method, path, _handle)
    # The local-WebRTC signaling WebSocket bypasses the core entirely (see
    # local_webrtc_signaling's module docstring), so it is deliberately not in
    # ROUTES; it is only registered when a viewer registry was actually
    # constructed (ADMIN_LOCAL_WEBRTC_ENABLED, default on).
    if admin_app.local_webrtc_viewers is not None:
        app.router.add_get('/api/live/webrtc', _handle_local_webrtc_ws)
    # Anything not registered above still goes through the core, so unknown
    # paths/methods get the core's own 404 (and its redaction) instead of an
    # aiohttp-generated page.
    app.router.add_route('*', '/{tail:.*}', _handle)
    return app


# --------------------------------------------------------------------------- #
# Mode / port / TLS selection
# --------------------------------------------------------------------------- #

def default_port(mode):
    """Return the documented bind port for ``mode`` (80 setup, 443 admin)."""
    return DEFAULT_SETUP_PORT if mode == 'setup' else DEFAULT_ADMIN_PORT


def resolve_mode(requested=None):
    """Resolve the application mode from an explicit value or provisioning state.

    An explicit valid mode always wins. Otherwise the device is ``admin`` once
    the persisted provisioning state is at/after ``claimed`` and ``setup``
    before that, so one entry point serves the correct surface across the claim
    transition.
    """
    if requested in VALID_MODES:
        return requested
    if requested not in (None, ''):
        log.warning('admin_app: ignoring invalid mode %r', requested)
    try:
        state = provisioning.ProvisioningState.load()
    except Exception:  # noqa: BLE001 - never fail startup on a bad state file
        return 'setup'
    return 'admin' if getattr(state, 'state', '') in _CLAIMED_STATES else 'setup'


def _configured_device_id(device_path=None):
    """Derive the stable device id for the setup SSID / admin hostname, or ``''``.

    Delegates to :func:`provisioning.resolve_device_id`, which prefers the
    configured ``device.toml`` fingerprint and otherwise falls back to the raw
    ``wlan0`` MAC or the persisted random seed. That fallback is what gives an
    unclaimed device (no ``device.toml`` yet) a stable identity for the setup
    hotspot. A missing/unreadable document yields ``''`` so the wizard degrades
    gracefully rather than crashing startup.
    """
    return provisioning.resolve_device_id(device_path)


def _default_runtime_client():
    """Return the bounded runtime-IPC client for the configured socket path.

    The socket path is overridable through ``RUNTIME_SOCKET_PATH`` for a
    non-standard install or a test. The client itself opens no connection until
    a request is made.
    """
    socket_path = (
        os.environ.get('RUNTIME_SOCKET_PATH') or runtime_ipc.DEFAULT_SOCKET_PATH
    )
    return runtime_ipc.RuntimeClient(socket_path)


def _default_dashboard_provider():
    """Return the bounded runtime-IPC dashboard provider (WP-UI2; AC-4).

    The admin process never constructs a ``CameraState``: it reads the live
    runtime through :mod:`runtime_ipc`. :class:`dashboard.DashboardProvider`
    caches the payload on a daemon thread, so the single admin-core worker
    never blocks on a slow or absent runtime.
    """
    socket_path = (
        os.environ.get('RUNTIME_SOCKET_PATH') or runtime_ipc.DEFAULT_SOCKET_PATH
    )
    client = runtime_ipc.RuntimeClient(socket_path)
    return dashboard.DashboardProvider(client)


def _default_live_monitor():
    """Return the one shared local live-monitor producer (WP-UI5; AC-10).

    The producer is lazy: constructing it starts no thread. The first
    authenticated viewer lease starts the single producer, and it tears itself
    down after the last lease expires. It reads only the existing ``stream_mux``
    H.264 fan-out through :func:`camera.capture_jpeg`; it never opens libcamera.
    """
    return live_monitor.LiveMonitor()


def _default_local_webrtc_viewers():
    """Return the shared local-WebRTC viewer registry, or ``None`` if disabled.

    Gated by ``ADMIN_LOCAL_WEBRTC_ENABLED`` (default on). Construction starts
    no thread and imports no GStreamer/PyGObject; the pipeline itself
    (:mod:`local_webrtc`) is imported lazily, only once a viewer is actually
    accepted by :func:`_handle_local_webrtc_ws`.
    """
    if not _local_webrtc_enabled():
        return None
    return local_webrtc_signaling.ViewerRegistry()


def _default_build_manager():
    """Return the one serialized timelapse build manager (WP-UI6; AC-13).

    Construction starts no thread. The manager owns the single job, preflight
    gates, the exclusive build lock, and the streaming writer.
    """
    return media_build.BuildManager()


def _default_network_controller(device_path=None, secrets_path=None):
    """Return the console's network controller (status, apply, hostname, NTP, scan).

    Reads are unprivileged (``nmcli``/``timedatectl``); every change goes through
    the fixed-verb helper. Construction runs no command and starts no thread.
    """
    return network_settings.NetworkController(
        apply_fn=privileged.network_apply,
        hostname_fn=privileged.hostname_apply,
        scan_fn=privileged.wifi_scan,
        ntp_fn=privileged.ntp_apply,
        device_path=device_path,
        secrets_path=secrets_path,
    )


def _wizard_wifi_scan():
    """Wizard scan: the root ``wifi-scan`` verb; a failed scan raises so the
    wizard reports it instead of showing an empty list as success."""
    networks = privileged.wifi_scan()
    if networks is None:
        raise RuntimeError('wifi scan unavailable')
    return networks


def _default_update_manager():
    """Return the update read/check/install control (WP-UI7; AC-15).

    Construction starts no thread. Both actions route through the fixed-verb
    privileged helper: ``check-update`` starts the report-only root updater unit,
    ``install-update`` starts the signed install unit. The manifest URL, signing
    key, channel, and command are fixed root-side and never reach this process.
    """
    return update_control.UpdateManager(
        check_fn=privileged.check_update,
        install_fn=privileged.install_update,
        identity_fn=app_version.build_identity,
    )


def _default_diagnostics_provider():
    """Return the bounded, redacted diagnostics provider (WP-UI7; AC-17).

    Construction starts no thread; the first authenticated request starts one
    daemon refresh, so the blocking ``journalctl`` never runs on the aiohttp
    event loop or the single admin-core worker.
    """
    return diagnostics.DiagnosticsProvider()


def _settings_action(client):
    """Build the runtime-IPC settings mutation callable (WP-UI3; AC-5).

    The returned callable runs on the admin-core worker (blocking socket I/O
    stays off the event loop) and normalizes the runtime's envelope into the
    shape :class:`admin_http.AdminApp` expects. A missing runtime is reported as
    ``degraded`` so the core answers 503 instead of a fabricated success.
    """

    def action(field, value):
        result = client.settings_set(field, value)
        if not isinstance(result, dict) or not result.get('ok'):
            reason = ''
            if isinstance(result, dict):
                reason = result.get('error', '')
            return {
                'ok': False,
                'error': reason or 'runtime unavailable',
                'degraded': True,
            }
        data = result.get('data') if isinstance(result.get('data'), dict) else {}
        settings = data.get('settings')
        return {
            'ok': bool(data.get('ok')),
            'error': data.get('reason', '') if not data.get('ok') else '',
            'changed': data.get('changed') or [],
            'settings': settings if isinstance(settings, dict) else {},
            'degraded': False,
        }

    return action


def build_admin_app(mode, *, device_path=None, secrets_path=None,
                    provisioning_path=None, hotspot_controller=None, probe=None,
                    start_camera=None, activate_station=None, mqtt_probe=None,
                    dashboard_provider=None, settings_actions=None,
                    live_monitor=None, local_webrtc_viewers=None, build_manager=None,
                    update_manager=None, diagnostics_provider=None, reboot_fn=None,
                    network_controller=None, wifi_scan=None):
    """Build the stdlib :class:`admin_http.AdminApp` with real dependencies.

    Paths default to the durable ``/data`` locations through the core's own
    defaults; the admin password hash is read lazily by the core from
    ``secrets.toml`` (never here, so no secret is handled in the transport).

    The wizard's finish path needs root-only actions, so the defaults route
    through the fixed-verb privileged helper (WP-R1/B2):

    * ``start_camera`` defaults to :func:`privileged.start_camera`, which starts
      ``pibuddycam.target`` through ``pibuddycam-priv start-camera``; the
      ``Conflicts=`` edges then stop ``pibuddycam-provisioning.service`` and the
      camera target pulls ``pibuddycam-admin.service`` (AC-12).
    * ``activate_station`` defaults to :func:`privileged.activate_station`, which
      creates/activates the Wi-Fi station profile before the camera starts so a
      claimed device comes up online.
    * the default hotspot controller is
      :class:`privileged.PrivilegedHotspot`: its ``start``/``stop`` need root,
      while ``status``/``is_active`` stay read-only :mod:`hotspot` queries.
    """
    try:
        state = provisioning.ProvisioningState.load(
            provisioning_path or provisioning.PROVISIONING_PATH)
    except Exception:  # noqa: BLE001
        state = None
    return admin_http.AdminApp(
        mode=mode,
        provisioning_state=state,
        device_id=_configured_device_id(device_path),
        hotspot=(
            hotspot_controller if hotspot_controller is not None
            else privileged.PrivilegedHotspot()
        ),
        probe=probe if probe is not None else camera_probe.probe,
        start_camera=(
            start_camera if start_camera is not None else privileged.start_camera
        ),
        activate_station=(
            activate_station if activate_station is not None
            else privileged.activate_station
        ),
        mqtt_probe=(
            mqtt_probe if mqtt_probe is not None else _DEFAULT_MQTT_PROBE
        ),
        dashboard_provider=(
            dashboard_provider if dashboard_provider is not None
            else _default_dashboard_provider()
        ),
        settings_actions=(
            settings_actions if settings_actions is not None
            else _settings_action(_default_runtime_client())
        ),
        live_monitor=(
            live_monitor if live_monitor is not None
            else _default_live_monitor()
        ),
        local_webrtc_viewers=(
            local_webrtc_viewers if local_webrtc_viewers is not None
            else _default_local_webrtc_viewers()
        ),
        build_manager=(
            build_manager if build_manager is not None
            else _default_build_manager()
        ),
        update_manager=(
            update_manager if update_manager is not None
            else _default_update_manager()
        ),
        diagnostics_provider=(
            diagnostics_provider if diagnostics_provider is not None
            else _default_diagnostics_provider()
        ),
        reboot_fn=(
            reboot_fn if reboot_fn is not None else privileged.reboot
        ),
        network_controller=(
            network_controller if network_controller is not None
            else _default_network_controller(device_path, secrets_path)
        ),
        wifi_scan=wifi_scan if wifi_scan is not None else _wizard_wifi_scan,
        device_path=device_path,
        secrets_path=secrets_path,
        provisioning_path=provisioning_path,
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def run(mode=None, *, host=None, port=None, ssl_context=None, admin_app=None):
    """Serve the admin/provisioning UI; blocks until shutdown.

    ``mode``/``host``/``port``/``ssl_context`` are all injectable for embedding
    and hardware bring-up. When omitted, mode is resolved from the persisted
    provisioning state, host from ``ADMIN_HOST`` (default :data:`DEFAULT_BIND_HOST`),
    port from ``ADMIN_PORT`` (default :func:`default_port`), and TLS from
    ``ADMIN_TLS_CERT``/``ADMIN_TLS_KEY``.

    Admin mode **fails closed**: a missing or invalid TLS configuration raises
    ``SystemExit`` so the unit fails rather than serving credentials over
    plaintext HTTP on :443. Setup mode stays plain HTTP on the captive portal.
    """
    resolved = resolve_mode(mode)
    if admin_app is None:
        admin_app = build_admin_app(resolved)
    if host is None:
        host = os.environ.get('ADMIN_HOST') or DEFAULT_BIND_HOST
    if port is None:
        port = _port_from_env(resolved)
    try:
        ssl_context = admin_tls.resolve_server_tls(resolved, ssl_context)
    except admin_tls.TlsConfigurationError as e:
        log.critical('admin_app: refusing to start: %s', e)
        raise SystemExit(1)
    scheme = 'https' if ssl_context is not None else 'http'
    log.info('admin_app: %s UI listening on %s://%s:%s', resolved, scheme, host, port)
    # No aiohttp access log: the core already logs a redacted request line, and
    # the default access log would print query strings.
    web.run_app(
        create_app(admin_app),
        host=host,
        port=port,
        ssl_context=ssl_context,
        access_log=None,
        print=None,
    )
    return 0


def _port_from_env(mode, env=None):
    """Return the configured bind port, or the mode default."""
    env = os.environ if env is None else env
    raw = env.get('ADMIN_PORT')
    if raw:
        try:
            port = int(raw)
        except (TypeError, ValueError):
            log.warning('admin_app: ignoring invalid ADMIN_PORT')
        else:
            if 0 < port < 65536:
                return port
            log.warning('admin_app: ignoring out-of-range ADMIN_PORT')
    return default_port(mode)


def main(argv=None):
    """Command-line entry point used by ``pibuddycam-admin.service``.

    ``ADMIN_MODE`` (or ``--mode``) selects ``setup``/``admin``; when neither is
    given the mode is resolved from the persisted provisioning state. This keeps
    one unit correct across the claim transition.
    """
    logging.basicConfig(
        level=os.environ.get('ADMIN_LOG_LEVEL', 'INFO').upper(),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    parser = argparse.ArgumentParser(description='PiBuddyCam admin/provisioning UI')
    parser.add_argument('--mode', choices=VALID_MODES, default=None)
    parser.add_argument('--host', default=None)
    parser.add_argument('--port', type=int, default=None)
    args = parser.parse_args(argv)
    mode = args.mode or os.environ.get('ADMIN_MODE')
    return run(mode=mode, host=args.host, port=args.port)


if __name__ == '__main__':
    image_guard.exit_if_legacy_image()
    raise SystemExit(main())
