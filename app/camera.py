import subprocess
import tempfile
import os
import glob
import time

def capture_jpeg(width=1920, height=1080):
    # Grab a frame from the always-running mux stream (port 8888).
    # libcamera is single-consumer; rpicam-source owns the sensor via stream_mux.py.
    # 'timeout' stops the pipeline after a frame is written. Hardware-found: 5s
    # was too short on a Pi Zero 2 W (OV5647 @1080p) -- GStreamer startup plus
    # the first keyframe exceeded it, so multifilesink wrote nothing and every
    # snapshot failed with "no frame captured". 10s proved reliable.
    with tempfile.TemporaryDirectory() as d:
        pattern = os.path.join(d, 'snap%05d.jpg')
        subprocess.run(
            ['timeout', '10',
             'gst-launch-1.0', '-q',
             'tcpclientsrc', 'host=127.0.0.1', 'port=8888', 'do-timestamp=true',
             '!', 'h264parse',
             '!', 'openh264dec',
             '!', 'videoconvert',
             '!', 'jpegenc', 'quality=95',  # GAP-SNAPSHOT-03: firmware JPEG quality is 95
             '!', 'multifilesink', f'location={pattern}'],
            capture_output=True, timeout=13
        )
        files = sorted(glob.glob(os.path.join(d, 'snap*.jpg')))
        if not files:
            raise RuntimeError('capture_jpeg: no frame captured')
        data = open(files[-1], 'rb').read()
        if len(data) < 100:
            raise RuntimeError('capture_jpeg: empty frame')
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
