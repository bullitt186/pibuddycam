"""Local-network WebRTC pipeline for the admin console's live-video view.

Unlike :mod:`webrtc` (the cloud-facing ``PrusaWebRTC``: one long-lived
instance that repeatedly tears down and re-offers to sequential Connect
viewers), each admin console viewer gets its own, single-use
:class:`LocalWebRTC` instance: the admin's WebSocket signaling handler
(``admin_app.py``) creates one when a viewer slot is acquired and tears it
down when that viewer's WebSocket closes. There is no "teardown the previous
session for a new offer" logic here because there is no previous session to
replace — a second concurrent viewer is a second instance, gated by
:class:`local_webrtc_signaling.ViewerRegistry`'s cap, not a second offer on
this one.

Reads the shared mux stream's *unpatched* port ``:8888`` (not ``:8889``,
which is SPS-patched only for Prusa's picky cloud libdatachannel answerer) —
a real browser's WebRTC stack accepts the camera's native baseline SPS as-is,
so none of ``webrtc.py``'s SDP-munging (``_munge_offer``/``_strip_sprop``) is
needed here. There is also no STUN/TURN server: this is a same-LAN peer, so
webrtcbin's default host ICE candidates are sufficient, matching
``rtsp_server.py``'s equally simple local-only pipeline.

This module is Pi-only (imports PyGObject/GStreamer) and, like ``webrtc.py``,
is not exercised by the host unit-test suite; ``local_webrtc_signaling.py``
holds the pure policy this module is driven by, and is where the automated
coverage lives.
"""
import asyncio
import logging
import threading

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstWebRTC, GstSdp, GLib

import webrtc_lifecycle

Gst.init(None)
log = logging.getLogger('prusa-cam.local_webrtc')

#: The mux's verbatim (unpatched) H.264 stream; see module docstring.
MUX_PORT = 8888

#: Fail a session that never reaches ICE CONNECTED/COMPLETED within this long.
CONNECT_WATCHDOG_SECONDS = 30

#: Grace period after ICE reports DISCONNECTED before treating it as ended;
#: ICE can recover on its own within this window (mirrors ``webrtc.py``).
DISCONNECT_GRACE_SECONDS = 15


class LocalWebRTC:
    """One single-use local WebRTC session for one admin-console viewer.

    ``on_offer(sdp_text)``, ``on_ice_candidate(candidate, mline_index)`` and
    ``on_ended(reason)`` are coroutine functions scheduled onto ``loop`` (the
    admin process's asyncio event loop) from the GLib thread that actually
    owns the pipeline — the same cross-thread marshalling pattern
    ``webrtc.py``'s ``PrusaWebRTC`` uses for the cloud path.
    """

    def __init__(self, on_offer, on_ice_candidate, on_ended, loop):
        self._on_offer = on_offer
        self._on_ice = on_ice_candidate
        self._on_ended = on_ended
        self._loop = loop

        self._pipe = None
        self._webrtc = None
        self._glib_loop = None
        self._glib_thread = None
        self._offer_started = False
        self._ended_notified = False
        self._disconnect_timeout_id = None
        self._connect_watchdog_id = None

    def start(self):
        """Start the GLib loop and build the pipeline. Not idempotent/reusable."""
        if self._glib_loop is not None:
            return
        self._glib_loop = GLib.MainLoop()
        self._glib_thread = threading.Thread(
            target=self._glib_loop.run, name='local-webrtc', daemon=True)
        self._glib_thread.start()

        pipeline_str = (
            f'tcpclientsrc host=127.0.0.1 port={MUX_PORT} do-timestamp=true '
            '! h264parse config-interval=-1 '
            '! rtph264pay config-interval=1 pt=96 '
            '! application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000 '
            '! webrtcbin name=webrtc bundle-policy=max-bundle'
        )
        self._pipe = Gst.parse_launch(pipeline_str)
        self._webrtc = self._pipe.get_by_name('webrtc')

        def connect(signal, handler):
            try:
                self._webrtc.connect(signal, handler)
            except Exception as e:
                log.warning(f'local webrtc: could not connect {signal}: {e}')

        connect('on-ice-candidate', self._on_ice_candidate_cb)
        # webrtcbin exposes ice-connection-state as a readable property and has
        # no on-ice-connection-state-change signal; use the GObject notify, same
        # as webrtc.py.
        connect('notify::ice-connection-state', self._on_notify_ice_state)
        connect('on-negotiation-needed', self._on_negotiation_needed)

        self._connect_watchdog_id = GLib.timeout_add_seconds(
            CONNECT_WATCHDOG_SECONDS, self._connect_watchdog)

        self._pipe.set_state(Gst.State.PLAYING)
        log.info('local webrtc: pipeline set to PLAYING')
        # Standard webrtcbin flow creates the offer from on-negotiation-needed;
        # keep a fallback in case it does not fire.
        GLib.timeout_add(3000, self._create_offer_timeout)

    def handle_answer(self, sdp_text):
        """Apply the browser's answer to the peer connection."""
        if self._webrtc is None:
            log.warning('local webrtc: answer received with no peer connection')
            return
        res, sdp_msg = GstSdp.SDPMessage.new_from_text(sdp_text)
        if res != GstSdp.SDPResult.OK:
            log.error(f'local webrtc: failed to parse SDP answer: {res}')
            return
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdp_msg)
        promise = Gst.Promise.new()
        self._webrtc.emit('set-remote-description', answer, promise)
        promise.wait()
        log.info('local webrtc: remote answer set')

    def add_ice_candidate(self, candidate, sdp_mline_index=0):
        if self._webrtc:
            self._webrtc.emit('add-ice-candidate', sdp_mline_index, candidate)

    def stop(self):
        """Tear down the pipeline and stop the GLib loop. Safe to call once."""
        self._cancel_timeout('_disconnect_timeout_id')
        self._cancel_timeout('_connect_watchdog_id')
        if self._webrtc is not None:
            try:
                self._webrtc.disconnect_by_func(self._on_notify_ice_state)
            except Exception:
                pass
        if self._pipe:
            self._pipe.set_state(Gst.State.NULL)
            self._pipe = None
            self._webrtc = None
        if self._glib_loop:
            self._glib_loop.quit()
            self._glib_loop = None

    # -- GLib-thread callbacks ---------------------------------------------

    def _on_negotiation_needed(self, element):
        log.info('local webrtc: negotiation needed; creating offer')
        self._create_offer()

    def _create_offer_timeout(self):
        if not self._offer_started:
            log.info('local webrtc: offer fallback timeout; creating offer')
            self._create_offer()
        return False

    def _create_offer(self):
        if self._webrtc is None or self._offer_started:
            return
        self._offer_started = True
        # The camera is a pure sender; offer sendonly rather than sendrecv.
        try:
            trans = self._webrtc.emit('get-transceiver', 0)
            if trans is not None:
                trans.set_property(
                    'direction', GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY)
        except Exception as e:
            log.warning(f'local webrtc: could not set transceiver direction: {e}')
        promise = Gst.Promise.new_with_change_func(self._on_offer_created)
        self._webrtc.emit('create-offer', None, promise)

    def _on_offer_created(self, promise):
        # Runs on the GLib main-loop thread; never block here with wait().
        reply = promise.get_reply()
        offer = reply.get_value('offer') if reply is not None else None
        if offer is None:
            log.error('local webrtc: offer creation returned no reply')
            return
        self._webrtc.emit('set-local-description', offer, None)
        log.info('local webrtc: local description set')
        self._notify(self._on_offer, offer.sdp.as_text())

    def _on_ice_candidate_cb(self, element, mline_index, candidate):
        self._notify(self._on_ice, candidate, mline_index)

    def _on_notify_ice_state(self, element, pspec):
        try:
            state = int(element.get_property('ice-connection-state'))
        except Exception as e:
            log.warning(f'local webrtc: could not read ice-connection-state: {e}')
            return
        self._on_ice_state_change(state)

    def _on_ice_state_change(self, state):
        """Handle an ICE connection-state change (GLib thread).

        CONNECTED/COMPLETED means media can flow; DISCONNECTED gets a grace
        period because ICE may recover, while FAILED/CLOSED are terminal.
        """
        log.info(f'local webrtc: ICE connection state: {state}')
        if state in webrtc_lifecycle.ICE_CONNECTED:
            self._cancel_timeout('_disconnect_timeout_id')
            self._cancel_timeout('_connect_watchdog_id')
            return
        reason = webrtc_lifecycle.end_reason(state)
        if reason == 'ice-disconnected':
            if self._disconnect_timeout_id is None:
                self._disconnect_timeout_id = GLib.timeout_add_seconds(
                    DISCONNECT_GRACE_SECONDS, self._check_disconnected)
        elif reason in ('ice-failed', 'ice-closed'):
            self._notify_ended(reason)

    def _check_disconnected(self):
        """Resolve a DISCONNECTED grace period after the timeout (GLib thread)."""
        self._disconnect_timeout_id = None
        state = None
        if self._webrtc is not None:
            try:
                state = int(self._webrtc.get_property('ice-connection-state'))
            except Exception as e:
                log.warning(f'local webrtc: could not read ICE connection state: {e}')
        if state in webrtc_lifecycle.ICE_ENDED or state == webrtc_lifecycle.ICE_DISCONNECTED:
            self._notify_ended('ice-disconnected')
        return False

    def _connect_watchdog(self):
        """Fail the session if ICE never connected (GLib thread)."""
        self._connect_watchdog_id = None
        self._notify_ended('no-ice-connection')
        return False

    def _notify_ended(self, reason):
        if self._ended_notified:
            return
        self._ended_notified = True
        log.info(f'local webrtc: session ended ({reason})')
        self._notify(self._on_ended, reason)

    def _cancel_timeout(self, attr):
        timeout_id = getattr(self, attr, None)
        if timeout_id is not None:
            try:
                GLib.source_remove(timeout_id)
            except Exception as e:
                log.warning(f'local webrtc: could not remove timeout {attr}={timeout_id}: {e}')
            setattr(self, attr, None)

    def _notify(self, callback, *args):
        if self._loop and callback:
            self._loop.call_soon_threadsafe(
                asyncio.ensure_future, callback(*args))


__all__ = ['CONNECT_WATCHDOG_SECONDS', 'DISCONNECT_GRACE_SECONDS', 'LocalWebRTC', 'MUX_PORT']
