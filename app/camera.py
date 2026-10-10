import subprocess
import tempfile
import os
import glob
import time

#: Upper bound for one capture. Hardware-found: 5 s was too short on a Pi Zero 2 W
#: (OV5647 @1080p) because GStreamer startup plus the first keyframe exceeded it;
#: 10 s proved reliable. It is only a ceiling now: a frame is returned as soon as one
#: has been written completely.
CAPTURE_TIMEOUT = 10.0
_JPEG_EOI = b'\xff\xd9'


def _first_jpeg(directory, final=False):
    """Return the first complete JPEG in ``directory``, or ``None``.

    ``multifilesink`` writes each frame as it is encoded, so a file can still be
    half written; a JPEG is complete when it ends with the EOI marker. With
    ``final`` (the writer has stopped) the newest sizeable file is taken instead.
    """
    files = sorted(glob.glob(os.path.join(directory, 'snap*.jpg')))
    if final:
        files = files[-1:]
    for path in files:
        try:
            with open(path, 'rb') as handle:
                data = handle.read()
        except OSError:
            continue
        if len(data) >= 100 and (final or data.endswith(_JPEG_EOI)):
            return data
    return None


def capture_jpeg(width=1920, height=1080, *, timeout=CAPTURE_TIMEOUT,
                 popen=subprocess.Popen, clock=time.monotonic, sleep=time.sleep):
    """Grab one frame from the always-running mux stream (port 8888).

    libcamera is single-consumer; rpicam-source owns the sensor via stream_mux.py,
    so this only decodes the mux's H.264 and never touches the camera. Returns as
    soon as the first complete frame is written (a second or two instead of a
    fixed 10 s run); the mux bootstraps a new client with its last keyframe, which
    is at most one GOP old. Raises :class:`RuntimeError` when nothing arrives
    within ``timeout``.
    """
    with tempfile.TemporaryDirectory() as d:
        pattern = os.path.join(d, 'snap%05d.jpg')
        proc = popen(
            ['gst-launch-1.0', '-q',
             'tcpclientsrc', 'host=127.0.0.1', 'port=8888', 'do-timestamp=true',
             '!', 'h264parse',
             '!', 'openh264dec',
             '!', 'videoconvert',
             '!', 'jpegenc', 'quality=95',  # GAP-SNAPSHOT-03: firmware JPEG quality is 95
             '!', 'multifilesink', f'location={pattern}'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = clock() + timeout
            data = _first_jpeg(d)
            while data is None and proc.poll() is None and clock() < deadline:
                sleep(0.05)
                data = _first_jpeg(d)
            if data is None:
                _stop(proc)
                data = _first_jpeg(d, final=True)
        finally:
            _stop(proc)
        if data is None:
            raise RuntimeError('capture_jpeg: no frame captured')
        return data


#: First decoded frame is the mux's cached bootstrap keyframe (up to one GOP old),
#: so it is never a candidate for a triggered capture.
BOOTSTRAP_FRAMES = 1


def pick_fresh_frame(frames, not_before, complete):
    """Choose the first live frame written at or after ``not_before``.

    ``frames`` is ``[(path, mtime), ...]`` in write order. The first
    :data:`BOOTSTRAP_FRAMES` entries are skipped: ``stream_mux`` bootstraps every
    new client with its cached last keyframe, which can predate the trigger
    (before the print head was parked). ``complete`` says the writer has
    finished; otherwise the newest file may still be half-written and is only a
    candidate when a later file exists. Returns the path or ``None``.
    """
    live = frames[BOOTSTRAP_FRAMES:]
    for index, (path, mtime) in enumerate(live):
        if mtime < not_before:
            continue
        if complete or index < len(live) - 1:
            return path
    return None


def capture_jpeg_after(t_trigger, width=1920, height=1080, *, settle=0.5,
                       timeout=8.0, popen=subprocess.Popen, clock=time.time,
                       sleep=time.sleep):
    """Capture a frame whose content was produced after ``t_trigger + settle``.

    Unlike :func:`capture_jpeg` (which returns whatever the last frame of a fixed
    10 s run is), this returns as soon as a live frame newer than the trigger has
    been written, so a layer-change pulse yields the frame *after* the head parked
    with the smallest possible latency. ``t_trigger`` is a ``time.time()`` value.
    Raises :class:`RuntimeError` when no such frame appears within ``timeout``.
    """
    not_before = t_trigger + settle
    with tempfile.TemporaryDirectory() as d:
        pattern = os.path.join(d, 'snap%05d.jpg')
        proc = popen(
            ['gst-launch-1.0', '-q',
             'tcpclientsrc', 'host=127.0.0.1', 'port=8888', 'do-timestamp=true',
             '!', 'h264parse',
             '!', 'openh264dec',
             '!', 'videoconvert',
             '!', 'jpegenc', 'quality=95',
             '!', 'multifilesink', f'location={pattern}'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        chosen = None
        deadline = clock() + timeout
        try:
            while True:
                files = sorted(glob.glob(os.path.join(d, 'snap*.jpg')))
                frames = []
                for path in files:
                    try:
                        frames.append((path, os.path.getmtime(path)))
                    except OSError:
                        continue
                chosen = pick_fresh_frame(frames, not_before, complete=False)
                if chosen is not None or clock() >= deadline:
                    break
                sleep(0.1)
            if chosen is None:
                # Last chance: the newest file is complete once the writer stops.
                _stop(proc)
                files = sorted(glob.glob(os.path.join(d, 'snap*.jpg')))
                frames = [(p, os.path.getmtime(p)) for p in files if os.path.exists(p)]
                chosen = pick_fresh_frame(frames, not_before, complete=True)
            if chosen is None:
                raise RuntimeError('capture_jpeg_after: no frame after the trigger')
            with open(chosen, 'rb') as handle:
                data = handle.read()
        finally:
            _stop(proc)
        if len(data) < 100:
            raise RuntimeError('capture_jpeg_after: empty frame')
        return data


def _stop(proc):
    """Terminate the capture pipeline and reap it (never raises)."""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
    except Exception:  # noqa: BLE001 - cleanup must never mask the result
        pass
