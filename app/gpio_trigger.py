"""GPIO timelapse trigger for the Prusa GPIO Hackerboard (Pi-only extension).

A Hackerboard output is open-drain: active means "connected to GND", otherwise it
floats. A Raspberry Pi input with its **internal pull-up to 3.3 V** therefore reads
it directly, and the Pi sees a **falling edge** when the printer activates the pin.
Two optional lines are watched:

* the **layer (shot) pin**: every falling edge is one frame request;
* the **recording pin**: level based, active (low) means "a print is running". Its
  falling edge opens a per-print session (folder + ``.active_session`` marker) and
  enables the timelapse; its rising edge disables it, closes the session and hands
  it to the automatic build.

Line access uses the Linux GPIO character-device **v2 uAPI** through ``fcntl.ioctl``
and ``struct`` only: no extra package. The chip is found by label
(``pinctrl-bcm2835`` on a Pi Zero 2 W). The fd/ioctl layer is injectable
(:class:`LinuxGpio`), so tests drive a fake chip.

Errors (no chip, permission denied, line busy) never raise into the runtime: they
become the ``error`` of :meth:`GpioTrigger.status`, which the console shows.

Stdlib only; importing runs no ioctl and touches no device.
"""
import errno
import fcntl
import glob
import logging
import os
import struct
import time

import timelapse

log = logging.getLogger('pibuddycam.gpio_trigger')

# --- linux/gpio.h (uAPI v2) ------------------------------------------------ #
_GPIO_MAGIC = 0xB4
_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(direction, number, size):
    return (direction << 30) | (size << 16) | (_GPIO_MAGIC << 8) | number


_CHIPINFO = struct.Struct('<32s32sI')                       # 68 bytes
_ATTR_ENTRY = struct.Struct('<IIQQ')                        # attr{id,pad,value} + mask
_LINE_VALUES = struct.Struct('<QQ')                         # bits, mask
_EVENT = struct.Struct('<QIIII24x')                         # 48 bytes

GPIO_GET_CHIPINFO_IOCTL = _ioc(_IOC_READ, 0x01, _CHIPINFO.size)

MAX_LINES = 64
MAX_ATTRS = 10
CONSUMER = b'pibuddycam'
# offsets[64] + consumer[32] + config(flags 8, num_attrs 4, pad 20, attrs 240)
# + num_lines 4 + event_buffer_size 4 + pad 20 + fd 4
_REQUEST_SIZE = 4 * MAX_LINES + 32 + (8 + 4 + 20 + 24 * MAX_ATTRS) + 4 + 4 + 20 + 4
GPIO_V2_GET_LINE_IOCTL = _ioc(_IOC_READ | _IOC_WRITE, 0x07, _REQUEST_SIZE)
GPIO_V2_LINE_GET_VALUES_IOCTL = _ioc(_IOC_READ | _IOC_WRITE, 0x0E, _LINE_VALUES.size)

FLAG_INPUT = 1 << 2
FLAG_EDGE_RISING = 1 << 4
FLAG_EDGE_FALLING = 1 << 5
FLAG_BIAS_PULL_UP = 1 << 8

ATTR_FLAGS = 1
ATTR_DEBOUNCE = 3

EDGE_RISING = 1
EDGE_FALLING = 2

#: Chip labels of the Pi SoC pin controllers (Zero 2 W = ``pinctrl-bcm2835``).
CHIP_LABELS = ('pinctrl-bcm2835', 'pinctrl-bcm2711', 'pinctrl-rp1')

SHOT_DEBOUNCE_US = 10_000
RECORD_DEBOUNCE_US = 50_000
#: Two layer pulses closer than this are one (bounce, or a G-code pulse doubled).
MIN_SHOT_GAP_SECONDS = 1.0


class GpioError(Exception):
    """A line could not be requested; the message is safe to show in the UI."""


def _error_message(exc):
    code = getattr(exc, 'errno', None)
    if code in (errno.EACCES, errno.EPERM):
        return 'permission denied (needs the gpio group and udev rule; new image)'
    if code == errno.EBUSY:
        return 'GPIO line is busy (used by another program)'
    if code in (errno.ENOENT, errno.ENODEV):
        return 'no GPIO chip found'
    if code == errno.EINVAL:
        return 'GPIO request rejected by the kernel'
    return 'GPIO unavailable'


def pack_request(lines):
    """Pack a ``gpio_v2_line_request`` for input lines with pull-up and edges.

    ``lines`` is ``[(offset, edge_flags, debounce_us), ...]`` (at most two are
    used here). Every line gets its own FLAGS attribute (input + pull-up + its edge
    flags) and a DEBOUNCE attribute, selected by a one-bit mask.
    """
    if not 1 <= len(lines) <= MAX_LINES:
        raise ValueError('1..64 lines')
    offsets = [offset for offset, _edges, _debounce in lines]
    offsets += [0] * (MAX_LINES - len(offsets))
    attrs = []
    for index, (_offset, edges, debounce) in enumerate(lines):
        mask = 1 << index
        attrs.append(_ATTR_ENTRY.pack(
            ATTR_FLAGS, 0, FLAG_INPUT | FLAG_BIAS_PULL_UP | edges, mask))
        attrs.append(_ATTR_ENTRY.pack(ATTR_DEBOUNCE, 0, debounce, mask))
    if len(attrs) > MAX_ATTRS:
        raise ValueError('too many attributes')
    attr_blob = b''.join(attrs).ljust(24 * MAX_ATTRS, b'\0')
    request = b''.join((
        struct.pack('<%dI' % MAX_LINES, *offsets),
        CONSUMER.ljust(32, b'\0'),
        struct.pack('<QI5I', FLAG_INPUT | FLAG_BIAS_PULL_UP, len(attrs), *([0] * 5)),
        attr_blob,
        struct.pack('<II5Ii', len(lines), 64, *([0] * 5), 0),
    ))
    assert len(request) == _REQUEST_SIZE, len(request)
    return request


class LinuxGpio:
    """The real GPIO character-device layer; every syscall is injectable."""

    def __init__(self, *, ioctl=fcntl.ioctl, open_fd=os.open, close_fd=os.close,
                 read_fd=os.read, list_chips=None):
        self._ioctl = ioctl
        self._open = open_fd
        self._close = close_fd
        self._read = read_fd
        self._list_chips = list_chips or (lambda: sorted(glob.glob('/dev/gpiochip*')))

    def find_chip(self):
        """Return the ``/dev/gpiochipN`` whose label is a Pi SoC controller."""
        last = None
        for path in self._list_chips():
            try:
                fd = self._open(path, os.O_RDONLY | os.O_CLOEXEC)
            except OSError as e:
                last = e
                continue
            try:
                info = self._ioctl(fd, GPIO_GET_CHIPINFO_IOCTL, bytes(_CHIPINFO.size))
                label = _CHIPINFO.unpack(bytes(info)[:_CHIPINFO.size])[1]
            except OSError as e:
                last = e
                continue
            finally:
                self._close(fd)
            if label.split(b'\0', 1)[0].decode('ascii', 'replace') in CHIP_LABELS:
                return path
        raise GpioError(_error_message(last) if last else 'no GPIO chip found')

    def request(self, chip_path, lines):
        """Request ``lines`` on the chip; returns the line-request fd."""
        try:
            chip_fd = self._open(chip_path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError as e:
            raise GpioError(_error_message(e)) from None
        try:
            result = self._ioctl(chip_fd, GPIO_V2_GET_LINE_IOCTL, pack_request(lines))
            fd = struct.unpack('<i', bytes(result)[-4:])[0]
            os.set_blocking(fd, False)   # read_events runs from the event loop
            return fd
        except OSError as e:
            raise GpioError(_error_message(e)) from None
        finally:
            self._close(chip_fd)

    def get_values(self, fd, count):
        """Return the raw input bits of the first ``count`` lines (1 = high)."""
        mask = (1 << count) - 1
        out = self._ioctl(fd, GPIO_V2_LINE_GET_VALUES_IOCTL, _LINE_VALUES.pack(0, mask))
        return _LINE_VALUES.unpack(bytes(out)[:_LINE_VALUES.size])[0] & mask

    def read_events(self, fd):
        """Read pending edge events as ``[(offset, edge), ...]`` (non-blocking)."""
        try:
            blob = self._read(fd, _EVENT.size * 16)
        except BlockingIOError:
            return []
        events = []
        for start in range(0, len(blob) - _EVENT.size + 1, _EVENT.size):
            _ts, edge, offset, _seq, _lseq = _EVENT.unpack_from(blob, start)
            events.append((offset, edge))
        return events

    def close(self, fd):
        try:
            self._close(fd)
        except OSError:
            pass


class GpioTrigger:
    """Owns the GPIO lines, the rate limit and the recording-session logic.

    ``loop`` must provide ``add_reader``/``remove_reader`` (asyncio). Callbacks:

    * ``on_shot(t_wall)`` - a layer pulse was accepted (queue it);
    * ``set_enabled(bool)`` - flip ``timelapse_enabled`` through the coordinator;
    * ``build_session(name)`` - queue the automatic build of a closed session;
    * ``is_enabled()`` - the current ``timelapse_enabled``.
    """

    def __init__(self, *, backend, loop, on_shot, set_enabled, build_session,
                 is_enabled, timelapse_dir=timelapse.TIMELAPSE_DIR,
                 clock=time.monotonic, wall=time.time,
                 min_shot_gap=MIN_SHOT_GAP_SECONDS):
        self._backend = backend
        self._loop = loop
        self._on_shot = on_shot
        self._set_enabled = set_enabled
        self._build_session = build_session
        self._is_enabled = is_enabled
        self._dir = timelapse_dir
        self._clock = clock
        self._wall = wall
        self._min_gap = min_shot_gap
        self._fd = None
        self._config = None       # (shot_pin, record_pin) currently requested
        self._error = ''
        self._last_shot = None    # monotonic
        self._last_trigger_at = None
        self._latency = None
        self._triggers = 0
        self._ignored = 0
        self._recording = False

    # -- configuration --------------------------------------------------- #

    def configure(self, trigger, shot_pin, record_pin):
        """Arm for ``gpio`` with these pins, or release for ``interval``.

        Idempotent: an unchanged, healthy configuration is left alone. Failures
        are reported through :meth:`status`, never raised.
        """
        if trigger != 'gpio' or shot_pin is None:
            self.release()
            self._error = ''
            return
        wanted = (shot_pin, record_pin)
        if self._fd is not None and self._config == wanted:
            return
        self.release()
        lines = [(shot_pin, FLAG_EDGE_FALLING, SHOT_DEBOUNCE_US)]
        if record_pin is not None:
            lines.append((record_pin, FLAG_EDGE_RISING | FLAG_EDGE_FALLING,
                          RECORD_DEBOUNCE_US))
        try:
            chip = self._backend.find_chip()
            fd = self._backend.request(chip, lines)
        except GpioError as e:
            self._error = str(e)
            log.warning(f'GPIO trigger not armed: {e}')
            return
        except Exception as e:  # noqa: BLE001 - never crash the runtime
            self._error = 'GPIO unavailable'
            log.warning(f'GPIO trigger not armed: {type(e).__name__}')
            return
        self._fd = fd
        self._config = wanted
        self._error = ''
        self._last_shot = None
        try:
            self._loop.add_reader(fd, self._on_readable)
        except Exception as e:  # noqa: BLE001
            self._backend.close(fd)
            self._fd = None
            self._config = None
            self._error = 'GPIO unavailable'
            log.warning(f'GPIO trigger not armed: {type(e).__name__}')
            return
        log.info(f'GPIO trigger armed: layer pin {shot_pin}, record pin {record_pin}')
        if record_pin is not None:
            self._arm_recording()

    def release(self):
        """Stop watching and hand the lines back."""
        fd, self._fd = self._fd, None
        self._config = None
        if fd is None:
            return
        try:
            self._loop.remove_reader(fd)
        except Exception:  # noqa: BLE001
            pass
        self._backend.close(fd)
        log.info('GPIO trigger released')

    # -- events ---------------------------------------------------------- #

    def _on_readable(self):
        if self._fd is None:
            return
        try:
            events = self._backend.read_events(self._fd)
        except OSError as e:
            self._error = _error_message(e)
            self.release()
            return
        for offset, edge in events:
            self.handle_edge(offset, edge)

    def handle_edge(self, offset, edge):
        """Process one edge event (also the seam for tests)."""
        if self._config is None:
            return
        shot_pin, record_pin = self._config
        if offset == shot_pin and edge == EDGE_FALLING:
            self._shot()
        elif record_pin is not None and offset == record_pin:
            if edge == EDGE_FALLING:
                self._record_active()
            elif edge == EDGE_RISING:
                self._record_inactive()

    def _shot(self):
        if not self._is_enabled():
            self._ignored += 1
            return
        now = self._clock()
        if self._last_shot is not None and now - self._last_shot < self._min_gap:
            self._ignored += 1
            return
        self._last_shot = now
        self._triggers += 1
        self._last_trigger_at = self._wall()
        self._on_shot(self._last_trigger_at)

    # -- recording sessions ---------------------------------------------- #

    def _arm_recording(self):
        """Reconcile the session with the recording pin's level at arming."""
        try:
            bits = self._backend.get_values(self._fd, 2)
        except Exception as e:  # noqa: BLE001
            log.warning(f'GPIO record level unreadable: {type(e).__name__}')
            return
        if not bits & 0b10:        # low = active (open-drain output pulling down)
            self._record_active()
        else:
            self._close_and_build()

    def _record_active(self):
        """Open a session, or resume the one named in the marker."""
        name = timelapse.active_session(self._dir)
        if name is None:
            try:
                name = timelapse.open_session(self._dir, now=self._wall())
            except OSError as e:
                self._error = 'could not create the session folder'
                log.warning(f'GPIO session not opened: {e.__class__.__name__}')
                return
        self._recording = True
        self._set_enabled(True)
        log.info(f'GPIO recording active ({name})')

    def _record_inactive(self):
        self._recording = False
        self._set_enabled(False)
        self._close_and_build()

    def _close_and_build(self):
        self._recording = False
        name = timelapse.close_session(self._dir)
        if name is None:
            return
        try:
            has_frames = bool(timelapse.list_frames(os.path.join(self._dir, name)))
        except OSError:
            has_frames = False
        if has_frames:
            log.info(f'GPIO recording ended ({name}); queueing the build')
            self._build_session(name)
        else:
            log.info(f'GPIO recording ended ({name}); no frames, nothing to build')

    # -- status ---------------------------------------------------------- #

    def note_latency(self, seconds):
        """Record the last trigger -> stored frame latency (seconds)."""
        self._latency = round(float(seconds), 2)

    def status(self):
        """Bounded, secret-free status for the console."""
        session = timelapse.active_session(self._dir) if self._recording else None
        return {
            'armed': self._fd is not None,
            'error': self._error,
            'shot_pin': self._config[0] if self._config else None,
            'record_pin': self._config[1] if self._config else None,
            'last_trigger_at': self._last_trigger_at,
            'latency_seconds': self._latency,
            'triggers': self._triggers,
            'ignored': self._ignored,
            'recording': self._recording,
            'session': session,
        }
