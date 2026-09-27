"""Camera image rotation shared by the camera source and the runtime.

Pi-local setting (no firmware equivalent): an absolute clockwise rotation of the
sensor image in degrees. The chosen value is written to an EnvironmentFile that
the ``rpicam-source`` unit reads on (re)start, alongside ``quality.env``, so a
quality change never resets the rotation. ``/etc/prusa-cam`` is tmpfs; the
durable copy is ``state.json`` (``rotation`` key), materialized again at boot by
``persist_restore``.

Stdlib only and no file side effects on import.
"""
import os

ROTATIONS = (0, 90, 180, 270)
DEFAULT_ROTATION = 0

ROTATION_ENV = os.environ.get('PRUSA_ROTATION_ENV', '/etc/prusa-cam/rotation.env')


def valid_rotation(value):
    """Return ``value`` when it is an int in :data:`ROTATIONS`, else None."""
    if type(value) is not int or value not in ROTATIONS:
        return None
    return value


def is_transposed(rotation):
    """True when the rotation swaps width and height (90/270)."""
    return rotation in (90, 270)


def oriented(width, height, rotation):
    """Return the output ``(width, height)`` of a ``width``x``height`` sensor image."""
    if is_transposed(rotation):
        return height, width
    return width, height


def write_current(rotation, path=None):
    """Persist ``rotation`` for the source unit. Returns the value written.

    Atomic and fsync'd like ``quality.write_current`` so a concurrent read or a
    power cut never sees a half-written file. Invalid input writes the default.
    """
    path = path or ROTATION_ENV
    value = valid_rotation(rotation)
    if value is None:
        value = DEFAULT_ROTATION
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(f'CAM_ROTATION={value}\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(os.path.dirname(path) or '.', os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return value


def read_raw(path=None):
    """Return the raw bytes of the env file, or None when it is absent."""
    try:
        with open(path or ROTATION_ENV, 'rb') as f:
            return f.read()
    except FileNotFoundError:
        return None


def restore_raw(previous, path=None):
    """Restore the env file to ``previous`` bytes, or remove it when None."""
    path = path or ROTATION_ENV
    if previous is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'wb') as f:
        f.write(previous)


def parse(text):
    """Return the rotation from ``CAM_ROTATION=`` text, or the default."""
    for line in (text or '').splitlines():
        key, sep, value = line.strip().partition('=')
        if sep and key == 'CAM_ROTATION':
            try:
                parsed = valid_rotation(int(value))
            except ValueError:
                parsed = None
            return DEFAULT_ROTATION if parsed is None else parsed
    return DEFAULT_ROTATION


def read_current(path=None):
    """Return the persisted rotation; the default when missing or invalid."""
    try:
        with open(path or ROTATION_ENV) as f:
            return parse(f.read())
    except OSError:
        return DEFAULT_ROTATION


def apply(degrees, restart, path=None):
    """Write ``degrees`` and restart the camera pipeline; True on success.

    ``restart`` is a zero-argument callable returning a return code (0 = ok),
    e.g. ``quality_control.restart_services``. A failed write or restart
    restores the previous env file, so a later source restart cannot pick up a
    rotation the runtime reported as rejected.
    """
    if valid_rotation(degrees) is None:
        return False
    previous = read_raw(path)
    try:
        write_current(degrees, path)
        returncode = restart()
    except Exception:
        returncode = None
    if returncode != 0:
        try:
            restore_raw(previous, path)
        except OSError:
            pass
        return False
    return True
