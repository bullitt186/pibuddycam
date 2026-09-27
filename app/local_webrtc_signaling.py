"""Local-only WebRTC signaling policy for the admin console's live-video view.

This is the pure-stdlib policy layer behind the admin console's live WebRTC
view: a bounded viewer-slot counter plus validation of the tiny JSON signaling
protocol exchanged over the admin's local WebSocket. It never imports
PyGObject/GStreamer, so it is fully host-testable — mirroring the existing
split between :mod:`webrtc_lifecycle` (pure policy, unit-tested) and
:mod:`webrtc` (the GStreamer pipeline, Pi-only).

This has nothing to do with Prusa's cloud WebRTC signaling (Socket.IO,
protobuf ``WebRTCMessage``, handled in ``main.py``/``webrtc.py``). It is a new,
local-network-only protocol between a browser tab open on the admin console
and a GStreamer pipeline running inside the admin process, so the message
shape below is plain JSON and owes nothing to the firmware's wire format.

Viewer accounting is deliberately simpler than :class:`live_monitor.LiveMonitor`:
the snapshot monitor infers a polling viewer's presence from repeated requests
and needs a lease TTL to notice one going away between polls. A local WebRTC
viewer instead holds one long-lived WebSocket connection for the life of its
peer connection, so liveness is the transport itself (TCP plus the
WebSocket's own ping/pong heartbeat) — a slot is simply held from
:meth:`ViewerRegistry.try_acquire` until the caller's own ``finally`` block
calls :meth:`ViewerRegistry.release`, with no separate expiry to track.
"""
import json
import threading

#: Each accepted viewer gets its own GStreamer webrtcbin pipeline and its own
#: TCP client to the shared mux stream — much heavier per-viewer than the
#: snapshot monitor's JPEG capture, so start conservative.
DEFAULT_MAX_VIEWERS = 1

#: Largest accepted SDP payload (offer/answer). Real SDP bodies are a few KB;
#: this is a generous bound that still rejects a garbage-sized payload before
#: it reaches GStreamer.
MAX_SDP_BYTES = 64 * 1024

#: Largest accepted single ICE candidate string.
MAX_CANDIDATE_BYTES = 4 * 1024

#: Message types a browser client may send.
CLIENT_MESSAGE_TYPES = frozenset({'answer', 'ice', 'stop'})


class SignalingError(ValueError):
    """Raised for a malformed, oversized, or unrecognized client message."""


def parse_client_message(raw):
    """Validate one browser -> server signaling message.

    Returns ``(msg_type, payload)`` where ``payload`` holds only the fields
    that type defines. Raises :class:`SignalingError` for anything malformed,
    oversized, or of an unrecognized type, so a bad payload is rejected before
    it can ever reach the GStreamer pipeline.
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise SignalingError('payload is not valid utf-8') from exc
    if not isinstance(raw, str):
        raise SignalingError('payload must be text')
    if not raw or len(raw) > MAX_SDP_BYTES:
        raise SignalingError('payload is empty or too large')
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SignalingError('payload is not valid JSON') from exc
    if not isinstance(data, dict):
        raise SignalingError('payload must be a JSON object')

    msg_type = data.get('type')
    if msg_type not in CLIENT_MESSAGE_TYPES:
        raise SignalingError(f'unrecognized message type: {msg_type!r}')

    if msg_type == 'answer':
        sdp = data.get('sdp')
        if not isinstance(sdp, str) or not sdp or len(sdp) > MAX_SDP_BYTES:
            raise SignalingError('answer missing/invalid sdp')
        return msg_type, {'sdp': sdp}

    if msg_type == 'ice':
        candidate = data.get('candidate')
        if not isinstance(candidate, str) or len(candidate) > MAX_CANDIDATE_BYTES:
            raise SignalingError('ice missing/invalid candidate')
        mline = data.get('sdpMLineIndex', 0)
        if not isinstance(mline, int) or isinstance(mline, bool) or mline < 0:
            raise SignalingError('ice missing/invalid sdpMLineIndex')
        return msg_type, {'candidate': candidate, 'sdpMLineIndex': mline}

    return msg_type, {}


class ViewerRegistry:
    """Bounded slot counter for concurrent local WebRTC viewers."""

    def __init__(self, *, max_viewers=DEFAULT_MAX_VIEWERS):
        if max_viewers < 1:
            raise ValueError('max_viewers must be at least 1')
        self._max_viewers = int(max_viewers)
        self._lock = threading.Lock()
        self._active = 0

    def try_acquire(self):
        """Reserve one viewer slot; return ``True``, or ``False`` at cap."""
        with self._lock:
            if self._active >= self._max_viewers:
                return False
            self._active += 1
            return True

    def release(self):
        """Free one viewer slot. Safe to call even when none is held."""
        with self._lock:
            if self._active > 0:
                self._active -= 1

    def metrics(self):
        """Return bounded, secret-free viewer counters for the status route."""
        with self._lock:
            return {'active': self._active, 'cap': self._max_viewers}


__all__ = [
    'CLIENT_MESSAGE_TYPES',
    'DEFAULT_MAX_VIEWERS',
    'MAX_CANDIDATE_BYTES',
    'MAX_SDP_BYTES',
    'SignalingError',
    'ViewerRegistry',
    'parse_client_message',
]
