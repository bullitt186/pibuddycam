"""Bounded shared local live-monitor producer (WP-UI5; AC-10/AC-11/AC-17).

The local Web UI must show a camera view **without ever opening a second
libcamera consumer** and without blocking the admin's asyncio event loop or its
single stdlib-core worker. The camera sensor is single-consumer and
``rpicam-source`` owns it; the only safe source is the always-running
``stream_mux`` H.264 fan-out on TCP ``8888``.

This module is the *one* shared producer for that view:

* a single daemon thread owns the decode/capture pipeline and writes only the
  **latest** validated JPEG into a one-slot buffer (no queue, no whole-stream
  buffering, no per-viewer decoder);
* viewers are bounded leases with a short TTL, so a viewer cap is enforced and
  the producer starts on the first viewer and tears itself down after an idle
  grace period when the last lease expires;
* malformed/partial upstream frames and overlarge frames are rejected before
  they can ever be served;
* it is stdlib-only and fully injectable, so the whole policy is host-testable
  with a fake producer and an injected clock (no GStreamer, no subprocess, no
  network).

The default producer reuses the existing mux-only JPEG capture
(:func:`camera.capture_jpeg`), which reads TCP ``8888``; it never invokes
``rpicam``/libcamera. Serving is deliberately snapshot-refresh (a client polls
:func:`LiveMonitor.latest` through an authenticated endpoint) rather than
multipart streaming, because the stdlib HTTP core cannot model a streaming
response and adding a transport-level stream would split the session
authorization path. See the WP-UI5 plan entry for the hardware decision gate.
"""
import logging
import threading
import time

log = logging.getLogger('prusa-cam.live_monitor')

#: Source label reported in metrics. A fixed string; no path or host is exposed.
SOURCE_LABEL = 'stream_mux:8888'

#: Default seconds between producer captures. Low-rate by design; the Pi budget
#: and the on-device benchmark decide whether it can be raised.
DEFAULT_INTERVAL_SECONDS = 1.0

#: Lower bound on the interval so a misconfiguration cannot spin the producer.
MIN_INTERVAL_SECONDS = 0.05

#: Fastest accepted cadence without hardware evidence: one frame per second.
#: ``interval`` is seconds between captures, so this is its upper bound.
MAX_INTERVAL_SECONDS = 1.0

#: Default idle grace: how long the producer keeps running after the last viewer
#: lease expires before it tears itself down.
DEFAULT_IDLE_GRACE_SECONDS = 5.0

#: Default viewer-lease TTL. A polling client refreshes this on every frame.
DEFAULT_VIEWER_TTL_SECONDS = 6.0

#: Cap on the exponential backoff applied after consecutive producer failures.
#: A failing or malformed upstream cannot tight-loop the producer thread.
DEFAULT_FAILURE_BACKOFF_MAX_SECONDS = 30.0

#: Maximum distinct concurrent viewers. Excess viewers are rejected (not
#: queued), so memory cannot grow with demand.
DEFAULT_MAX_VIEWERS = 4

#: A frame newer than this is reported ``live``; older is ``stale``.
DEFAULT_FRESH_SECONDS = 8.0

#: A frame older than this is never served (bounded stale age).
DEFAULT_MAX_STALE_SECONDS = 30.0

#: Smallest plausible complete JPEG. Anything shorter is treated as malformed.
MIN_JPEG_BYTES = 128

#: Largest frame accepted into the one-slot buffer.
DEFAULT_MAX_FRAME_BYTES = 2 * 1024 * 1024

#: Reported frame states.
STATE_LIVE = 'live'
STATE_STALE = 'stale'
STATE_UNAVAILABLE = 'unavailable'

_JPEG_SOI = b'\xff\xd8'
_JPEG_EOI = b'\xff\xd9'


def valid_jpeg(data, *, max_bytes=DEFAULT_MAX_FRAME_BYTES):
    """True only for a complete, bounded JPEG (SOI .. EOI).

    Rejects partial frames, wrong content types, empty data, and overlarge
    payloads. This is the single gate every upstream frame passes before it can
    be cached or served.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return False
    if isinstance(data, memoryview):
        data = data.tobytes()
    if not data or len(data) < MIN_JPEG_BYTES or len(data) > max_bytes:
        return False
    return bytes(data[:2]) == _JPEG_SOI and bytes(data[-2:]) == _JPEG_EOI


def default_producer():
    """Capture one JPEG from the shared mux stream (never libcamera/rpicam).

    Imports :mod:`camera` lazily so this module stays import-safe in host tests
    without GStreamer. ``camera.capture_jpeg`` reads TCP ``8888`` (the mux) and
    is the existing JPEG producer used by periodic snapshots.
    """
    from camera import capture_jpeg
    return capture_jpeg()


class LiveMonitor:
    """One shared, bounded JPEG producer plus a viewer-lease registry."""

    def __init__(self, producer=None, *, interval=DEFAULT_INTERVAL_SECONDS,
                 idle_grace=DEFAULT_IDLE_GRACE_SECONDS,
                 viewer_ttl=DEFAULT_VIEWER_TTL_SECONDS,
                 max_viewers=DEFAULT_MAX_VIEWERS,
                 fresh_seconds=DEFAULT_FRESH_SECONDS,
                 max_stale_seconds=DEFAULT_MAX_STALE_SECONDS,
                 max_frame_bytes=DEFAULT_MAX_FRAME_BYTES,
                 failure_backoff_max=DEFAULT_FAILURE_BACKOFF_MAX_SECONDS,
                 clock=time.monotonic):
        if interval < MIN_INTERVAL_SECONDS:
            raise ValueError('interval below the minimum')
        if interval > MAX_INTERVAL_SECONDS:
            # Conservative default: the shipped producer is never faster than
            # 1 fps. A faster cadence is a hardware-evidence decision.
            raise ValueError('interval above the 1 fps default bound')
        if idle_grace < 0:
            raise ValueError('idle_grace must be non-negative')
        if viewer_ttl <= 0:
            raise ValueError('viewer_ttl must be positive')
        if max_viewers < 1:
            raise ValueError('max_viewers must be at least 1')
        if fresh_seconds < 0 or max_stale_seconds < fresh_seconds:
            raise ValueError('stale bounds are invalid')
        if failure_backoff_max < interval:
            raise ValueError('failure_backoff_max must be >= interval')

        self._producer = producer if producer is not None else default_producer
        self._interval = float(interval)
        self._idle_grace = float(idle_grace)
        self._viewer_ttl = float(viewer_ttl)
        self._max_viewers = int(max_viewers)
        self._fresh_seconds = float(fresh_seconds)
        self._max_stale_seconds = float(max_stale_seconds)
        self._max_frame_bytes = int(max_frame_bytes)
        self._failure_backoff_max = float(failure_backoff_max)
        self._clock = clock

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._running = False
        # Monotonic generation so an exiting thread cannot clear the running
        # flag of a newer thread started after it.
        self._generation = 0

        # One-slot latest-frame buffer.
        self._jpeg = b''
        self._captured_at = None
        self._last_bytes = 0

        # Bounded counters (never grow with time).
        self._frames_produced = 0
        self._captures_failed = 0
        self._invalid_frames = 0
        # Consecutive failed/malformed captures; drives the backoff and resets
        # on the first valid frame.
        self._failure_streak = 0

        # viewer_id -> lease expiry (monotonic seconds).
        self._leases = {}

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self):
        with self._lock:
            return self._running

    def start(self):
        """Start the producer thread if it is not already running (idempotent)."""
        self._ensure_running()

    def stop(self, *, timeout=1.0):
        """Signal the producer to stop and wait briefly for it; never raises.

        A capture already in flight is bounded by the producer's own timeout, so
        the thread exits after at most one more frame. The join is best-effort:
        the thread is a daemon and cannot outlive the process.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=timeout)
            except Exception:  # noqa: BLE001 - shutdown must never raise
                pass
        with self._lock:
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None
                self._running = False

    # -- viewers -----------------------------------------------------------

    def touch(self, viewer_id):
        """Register/refresh one viewer lease; return ``False`` when over cap.

        A repeat touch from an already-leased viewer always succeeds and extends
        the lease, so a polling client is never rejected mid-view. Only a *new*
        viewer beyond ``max_viewers`` is refused.
        """
        if not isinstance(viewer_id, str) or not viewer_id:
            return False
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            if viewer_id not in self._leases and len(self._leases) >= self._max_viewers:
                return False
            self._leases[viewer_id] = now + self._viewer_ttl
        self._ensure_running()
        return True

    def release(self, viewer_id):
        """Drop a viewer lease early (best-effort; never raises)."""
        with self._lock:
            self._leases.pop(viewer_id, None)

    def viewer_count(self):
        """Return the number of unexpired viewer leases."""
        with self._lock:
            self._prune_locked(self._clock())
            return len(self._leases)

    def _prune_locked(self, now):
        expired = [key for key, expiry in self._leases.items() if expiry <= now]
        for key in expired:
            self._leases.pop(key, None)

    def _ensure_running(self):
        with self._lock:
            if (self._running and self._thread is not None
                    and self._thread.is_alive()):
                return
            # Clear the stop event only when actually starting a new thread, so
            # a viewer arriving after stop() can restart the producer without
            # reviving a thread that is already shutting down.
            self._stop.clear()
            # A fresh generation prevents an exiting thread from clearing the
            # running flag of the thread started after it.
            self._generation += 1
            generation = self._generation
            self._running = True
            self._thread = threading.Thread(
                target=self._run, args=(generation,),
                name='live-monitor', daemon=True)
            self._thread.start()

    # -- frame buffer ------------------------------------------------------

    def latest(self):
        """Return ``{state, jpeg, age_seconds}`` for the one-slot buffer.

        ``jpeg`` is ``None`` when there is no frame yet or the newest frame is
        older than the hard stale bound, so a stale frame can never be served as
        if it were live.
        """
        with self._lock:
            jpeg = self._jpeg
            captured = self._captured_at
        if not jpeg or captured is None:
            return {'state': STATE_UNAVAILABLE, 'jpeg': None, 'age_seconds': None}
        age = max(0.0, self._clock() - captured)
        if age > self._max_stale_seconds:
            return {'state': STATE_STALE, 'jpeg': None, 'age_seconds': age}
        state = STATE_LIVE if age <= self._fresh_seconds else STATE_STALE
        return {'state': state, 'jpeg': jpeg, 'age_seconds': age}

    def metrics(self):
        """Return bounded, secret-free producer/viewer metrics for acceptance.

        The payload intentionally contains only counters, durations, and fixed
        labels: no filesystem path, hostname, token, or frame bytes.
        """
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            running = self._running
            produced = self._frames_produced
            failed = self._captures_failed
            invalid = self._invalid_frames
            last_bytes = self._last_bytes
            captured = self._captured_at
            active = len(self._leases)
            failure_streak = self._failure_streak
        age = None if captured is None else max(0.0, self._clock() - captured)
        state = self.latest()['state']
        return {
            'source': SOURCE_LABEL,
            'interval_seconds': self._interval,
            'producer': {
                'running': running,
                'frames_produced': produced,
                'captures_failed': failed,
                'invalid_frames': invalid,
                'consecutive_failures': failure_streak,
                'failure_backoff_max_seconds': self._failure_backoff_max,
                'last_frame_bytes': last_bytes,
                'last_frame_age_seconds': age,
            },
            'viewers': {
                'active': active,
                'cap': self._max_viewers,
                'lease_seconds': self._viewer_ttl,
            },
            'frame': {
                'state': state,
                'fresh_seconds': self._fresh_seconds,
                'max_stale_seconds': self._max_stale_seconds,
            },
        }

    # -- producer thread ---------------------------------------------------

    def _has_viewers(self):
        with self._lock:
            self._prune_locked(self._clock())
            return bool(self._leases)

    def _capture_once(self):
        """Capture one frame; return True only when a valid frame was cached."""
        try:
            data = self._producer()
        except Exception:  # noqa: BLE001 - a failed capture must not kill the thread
            with self._lock:
                self._captures_failed += 1
            log.debug('live_monitor: capture failed', exc_info=True)
            return False
        if not valid_jpeg(data, max_bytes=self._max_frame_bytes):
            with self._lock:
                self._invalid_frames += 1
            log.debug('live_monitor: rejected malformed/oversized frame')
            return False
        with self._lock:
            self._jpeg = bytes(data)
            self._captured_at = self._clock()
            self._last_bytes = len(data)
            self._frames_produced += 1
        return True

    def _backoff_delay(self, ok):
        """Return the wait before the next capture.

        A valid frame resets the cadence to ``interval``. A failed or malformed
        frame grows an exponential, capped backoff, so a broken upstream can
        never tight-loop the producer thread.
        """
        with self._lock:
            if ok:
                self._failure_streak = 0
                return self._interval
            self._failure_streak += 1
            streak = self._failure_streak
        return min(
            self._failure_backoff_max,
            self._interval * (2 ** min(streak, 16)),
        )

    def _run(self, generation):
        idle_since = None
        try:
            while not self._stop.is_set():
                if not self._has_viewers():
                    if idle_since is None:
                        idle_since = self._clock()
                    elif self._clock() - idle_since >= self._idle_grace:
                        break
                    self._stop.wait(min(0.1, self._idle_grace) or 0.1)
                    continue
                idle_since = None
                ok = self._capture_once()
                self._stop.wait(self._backoff_delay(ok))
        finally:
            with self._lock:
                if self._generation == generation:
                    self._running = False


__all__ = [
    'DEFAULT_FAILURE_BACKOFF_MAX_SECONDS',
    'DEFAULT_FRESH_SECONDS',
    'DEFAULT_IDLE_GRACE_SECONDS',
    'DEFAULT_INTERVAL_SECONDS',
    'DEFAULT_MAX_FRAME_BYTES',
    'DEFAULT_MAX_STALE_SECONDS',
    'DEFAULT_MAX_VIEWERS',
    'DEFAULT_VIEWER_TTL_SECONDS',
    'LiveMonitor',
    'MAX_INTERVAL_SECONDS',
    'MIN_INTERVAL_SECONDS',
    'MIN_JPEG_BYTES',
    'SOURCE_LABEL',
    'STATE_LIVE',
    'STATE_STALE',
    'STATE_UNAVAILABLE',
    'default_producer',
    'valid_jpeg',
]
