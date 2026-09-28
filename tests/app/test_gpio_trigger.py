"""GPIO trigger: uAPI packing, chip discovery, rate limit, sessions, errors."""
import errno
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import gpio_trigger as gt  # noqa: E402
import timelapse  # noqa: E402


class FakeLoop:
    def __init__(self):
        self.readers = {}

    def add_reader(self, fd, callback):
        self.readers[fd] = callback

    def remove_reader(self, fd):
        self.readers.pop(fd, None)


class FakeBackend:
    """Stand-in for LinuxGpio: records requests, replays scripted events."""

    def __init__(self, level_bits=0b11, error=None):
        self.requests = []
        self.closed = []
        self.events = []
        self.level_bits = level_bits
        self.error = error
        self.next_fd = 40

    def find_chip(self):
        if self.error:
            raise self.error
        return '/dev/gpiochip0'

    def request(self, chip, lines):
        if self.error:
            raise self.error
        self.requests.append((chip, list(lines)))
        self.next_fd += 1
        return self.next_fd

    def get_values(self, fd, count):
        return self.level_bits

    def read_events(self, fd):
        events, self.events = self.events, []
        return events

    def close(self, fd):
        self.closed.append(fd)


class Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.loop = FakeLoop()
        self.backend = FakeBackend()
        self.shots = []
        self.enabled_calls = []
        self.enabled = [True]
        self.builds = []
        self.mono = [100.0]
        self.wall = [1_700_000_000.0]

    def make(self, backend=None, **kw):
        def set_enabled(flag):
            self.enabled_calls.append(flag)
            self.enabled[0] = flag
        args = dict(
            backend=backend or self.backend, loop=self.loop,
            on_shot=self.shots.append, set_enabled=set_enabled,
            build_session=self.builds.append, is_enabled=lambda: self.enabled[0],
            timelapse_dir=self.dir, clock=lambda: self.mono[0], wall=lambda: self.wall[0])
        args.update(kw)
        return gt.GpioTrigger(**args)

    def frame(self, session):
        path = os.path.join(self.dir, session)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, 'timelapse_00-00-01-000.jpg'), 'wb') as handle:
            handle.write(b'\xff\xd8\xff' + b'x' * 200)


class PackingTests(unittest.TestCase):
    def test_ioctl_numbers_match_the_kernel_header(self):
        self.assertEqual(gt.GPIO_GET_CHIPINFO_IOCTL, 0x8044B401)
        self.assertEqual(gt.GPIO_V2_GET_LINE_IOCTL, 0xC250B407)
        self.assertEqual(gt.GPIO_V2_LINE_GET_VALUES_IOCTL, 0xC010B40E)

    def test_request_layout_for_two_lines(self):
        blob = gt.pack_request([
            (17, gt.FLAG_EDGE_FALLING, 10_000),
            (27, gt.FLAG_EDGE_RISING | gt.FLAG_EDGE_FALLING, 50_000),
        ])
        self.assertEqual(len(blob), 592)
        offsets = struct.unpack_from('<64I', blob, 0)
        self.assertEqual(offsets[:3], (17, 27, 0))
        self.assertEqual(blob[256:256 + 10], b'pibuddycam')
        default_flags, num_attrs = struct.unpack_from('<QI', blob, 288)
        self.assertEqual(default_flags, gt.FLAG_INPUT | gt.FLAG_BIAS_PULL_UP)
        self.assertEqual(num_attrs, 4)
        attrs = [struct.unpack_from('<IIQQ', blob, 288 + 32 + 24 * i) for i in range(4)]
        # shot line: flags attribute with pull-up + falling edge, then debounce 10 ms
        self.assertEqual(attrs[0], (gt.ATTR_FLAGS, 0,
                                    gt.FLAG_INPUT | gt.FLAG_BIAS_PULL_UP | gt.FLAG_EDGE_FALLING,
                                    0b01))
        self.assertEqual(attrs[1], (gt.ATTR_DEBOUNCE, 0, 10_000, 0b01))
        # record line: both edges, debounce 50 ms
        self.assertEqual(attrs[2], (gt.ATTR_FLAGS, 0,
                                    gt.FLAG_INPUT | gt.FLAG_BIAS_PULL_UP
                                    | gt.FLAG_EDGE_RISING | gt.FLAG_EDGE_FALLING, 0b10))
        self.assertEqual(attrs[3], (gt.ATTR_DEBOUNCE, 0, 50_000, 0b10))
        num_lines, buffer_size = struct.unpack_from('<II', blob, 288 + 272)
        self.assertEqual((num_lines, buffer_size), (2, 64))

    def test_single_line_request(self):
        blob = gt.pack_request([(4, gt.FLAG_EDGE_FALLING, 10_000)])
        self.assertEqual(struct.unpack_from('<I', blob, 288 + 8)[0], 2)
        self.assertEqual(struct.unpack_from('<I', blob, 288 + 272)[0], 1)

    def test_pull_up_is_a_bias_flag_and_never_pull_down(self):
        self.assertEqual(gt.FLAG_BIAS_PULL_UP, 1 << 8)
        blob = gt.pack_request([(4, gt.FLAG_EDGE_FALLING, 10_000)])
        flags = struct.unpack_from('<Q', blob, 288 + 32 + 8)[0]
        self.assertFalse(flags & (1 << 9))     # BIAS_PULL_DOWN
        self.assertFalse(flags & (1 << 3))     # OUTPUT


class LinuxGpioTests(unittest.TestCase):
    def _chipinfo(self, label):
        return struct.pack('<32s32sI', b'gpiochip', label.encode(), 54)

    def test_finds_the_chip_by_label_across_chips(self):
        labels = {'/dev/gpiochip0': 'other', '/dev/gpiochip1': 'pinctrl-bcm2835'}
        current = {}

        def open_fd(path, flags):
            current['path'] = path
            return 7

        def ioctl(fd, request, buf):
            self.assertEqual(request, gt.GPIO_GET_CHIPINFO_IOCTL)
            return self._chipinfo(labels[current['path']])

        backend = gt.LinuxGpio(ioctl=ioctl, open_fd=open_fd, close_fd=lambda fd: None,
                               list_chips=lambda: sorted(labels))
        self.assertEqual(backend.find_chip(), '/dev/gpiochip1')

    def test_no_matching_chip(self):
        backend = gt.LinuxGpio(
            ioctl=lambda fd, req, buf: self._chipinfo('something-else'),
            open_fd=lambda p, f: 7, close_fd=lambda fd: None,
            list_chips=lambda: ['/dev/gpiochip0'])
        with self.assertRaises(gt.GpioError) as ctx:
            backend.find_chip()
        self.assertIn('no GPIO chip', str(ctx.exception))

    def test_permission_denied_is_reported_helpfully(self):
        def deny(path, flags):
            raise PermissionError(errno.EACCES, 'denied')
        backend = gt.LinuxGpio(open_fd=deny, list_chips=lambda: ['/dev/gpiochip0'])
        with self.assertRaises(gt.GpioError) as ctx:
            backend.find_chip()
        self.assertIn('permission denied', str(ctx.exception))
        self.assertIn('new image', str(ctx.exception))

    def test_request_returns_the_line_fd_and_makes_it_non_blocking(self):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        seen = {}

        def ioctl(fd, request, buf):
            seen['request'], seen['len'] = request, len(buf)
            return bytes(buf[:-4]) + struct.pack('<i', r)

        backend = gt.LinuxGpio(ioctl=ioctl, open_fd=lambda p, f: 9, close_fd=lambda fd: None)
        fd = backend.request('/dev/gpiochip0', [(17, gt.FLAG_EDGE_FALLING, 10_000)])
        self.assertEqual((fd, seen['request'], seen['len']), (r, gt.GPIO_V2_GET_LINE_IOCTL, 592))
        self.assertFalse(os.get_blocking(r))

    def test_busy_line(self):
        def ioctl(fd, request, buf):
            raise OSError(errno.EBUSY, 'busy')
        backend = gt.LinuxGpio(ioctl=ioctl, open_fd=lambda p, f: 9, close_fd=lambda fd: None)
        with self.assertRaises(gt.GpioError) as ctx:
            backend.request('/dev/gpiochip0', [(17, gt.FLAG_EDGE_FALLING, 10_000)])
        self.assertIn('busy', str(ctx.exception))

    def test_get_values_and_events(self):
        def ioctl(fd, request, buf):
            self.assertEqual(request, gt.GPIO_V2_LINE_GET_VALUES_IOCTL)
            mask = struct.unpack('<QQ', bytes(buf))[1]
            self.assertEqual(mask, 0b11)
            return struct.pack('<QQ', 0b10, mask)

        event = struct.pack('<QIIII24x', 123, gt.EDGE_FALLING, 17, 1, 1)
        backend = gt.LinuxGpio(ioctl=ioctl, read_fd=lambda fd, n: event * 2)
        self.assertEqual(backend.get_values(5, 2), 0b10)
        self.assertEqual(backend.read_events(5), [(17, gt.EDGE_FALLING)] * 2)

        def would_block(fd, n):
            raise BlockingIOError()
        self.assertEqual(gt.LinuxGpio(read_fd=would_block).read_events(5), [])


class ArmingTests(Harness):
    def test_shot_only_requests_one_line_and_registers_a_reader(self):
        trigger = self.make()
        trigger.configure('gpio', 17, None)
        self.assertEqual(self.backend.requests,
                         [('/dev/gpiochip0', [(17, gt.FLAG_EDGE_FALLING, 10_000)])])
        self.assertEqual(list(self.loop.readers), [41])
        status = trigger.status()
        self.assertTrue(status['armed'])
        self.assertEqual((status['shot_pin'], status['record_pin']), (17, None))

    def test_record_pin_adds_a_second_line(self):
        trigger = self.make()
        trigger.configure('gpio', 17, 27)
        lines = self.backend.requests[0][1]
        self.assertEqual(lines[1], (27, gt.FLAG_EDGE_RISING | gt.FLAG_EDGE_FALLING, 50_000))

    def test_interval_or_missing_pin_releases(self):
        trigger = self.make()
        trigger.configure('gpio', 17, None)
        trigger.configure('interval', 17, None)
        self.assertEqual(self.backend.closed, [41])
        self.assertEqual(self.loop.readers, {})
        self.assertFalse(trigger.status()['armed'])
        trigger.configure('gpio', None, None)
        self.assertFalse(trigger.status()['armed'])

    def test_unchanged_configuration_is_not_requested_again(self):
        trigger = self.make()
        trigger.configure('gpio', 17, 27)
        trigger.configure('gpio', 17, 27)
        self.assertEqual(len(self.backend.requests), 1)

    def test_changing_a_pin_releases_then_requests_again(self):
        trigger = self.make()
        trigger.configure('gpio', 17, None)
        trigger.configure('gpio', 22, None)
        self.assertEqual(len(self.backend.requests), 2)
        self.assertEqual(self.backend.closed, [41])
        self.assertEqual(list(self.loop.readers), [42])

    def test_release_is_idempotent(self):
        trigger = self.make()
        trigger.release()
        trigger.configure('gpio', 17, None)
        trigger.release()
        trigger.release()
        self.assertEqual(self.backend.closed, [41])

    def test_errors_become_status_not_exceptions(self):
        for error, text in (
            (gt.GpioError('permission denied (needs the gpio group)'), 'permission denied'),
            (gt.GpioError('GPIO line is busy'), 'busy'),
            (gt.GpioError('no GPIO chip found'), 'no GPIO chip'),
            (RuntimeError('boom'), 'GPIO unavailable'),
        ):
            trigger = self.make(backend=FakeBackend(error=error))
            trigger.configure('gpio', 17, None)
            status = trigger.status()
            self.assertFalse(status['armed'])
            self.assertIn(text, status['error'])

    def test_error_clears_after_a_successful_arm(self):
        backend = FakeBackend(error=gt.GpioError('busy'))
        trigger = self.make(backend=backend)
        trigger.configure('gpio', 17, None)
        backend.error = None
        trigger.configure('gpio', 17, None)
        self.assertEqual(trigger.status()['error'], '')
        self.assertTrue(trigger.status()['armed'])


class ShotTests(Harness):
    def setUp(self):
        super().setUp()
        self.trigger = self.make()
        self.trigger.configure('gpio', 17, None)

    def pulse(self):
        self.trigger.handle_edge(17, gt.EDGE_FALLING)

    def test_falling_edge_queues_a_shot_with_the_wall_time(self):
        self.pulse()
        self.assertEqual(self.shots, [self.wall[0]])
        self.assertEqual(self.trigger.status()['last_trigger_at'], self.wall[0])

    def test_rising_edge_and_other_pins_are_ignored(self):
        self.trigger.handle_edge(17, gt.EDGE_RISING)
        self.trigger.handle_edge(22, gt.EDGE_FALLING)
        self.assertEqual(self.shots, [])

    def test_pulses_within_a_second_are_one_shot(self):
        self.pulse()
        self.mono[0] += 0.4
        self.pulse()
        self.mono[0] += 0.5
        self.pulse()
        self.assertEqual(len(self.shots), 1)
        self.assertEqual(self.trigger.status()['ignored'], 2)
        self.mono[0] += 0.2
        self.pulse()
        self.assertEqual(len(self.shots), 2)

    def test_ignored_while_the_timelapse_is_disabled(self):
        self.enabled[0] = False
        self.pulse()
        self.assertEqual(self.shots, [])
        self.enabled[0] = True
        self.pulse()
        self.assertEqual(len(self.shots), 1)

    def test_events_are_read_from_the_line_fd_through_the_loop(self):
        self.backend.events = [(17, gt.EDGE_FALLING)]
        self.loop.readers[41]()
        self.assertEqual(len(self.shots), 1)

    def test_a_dead_fd_releases_and_reports(self):
        def broken(fd):
            raise OSError(errno.ENODEV, 'gone')
        self.backend.read_events = broken
        self.loop.readers[41]()
        status = self.trigger.status()
        self.assertFalse(status['armed'])
        self.assertIn('no GPIO chip', status['error'])

    def test_latency_is_reported(self):
        self.trigger.note_latency(4.2567)
        self.assertEqual(self.trigger.status()['latency_seconds'], 4.26)

    def test_released_trigger_ignores_late_edges(self):
        self.trigger.release()
        self.pulse()
        self.assertEqual(self.shots, [])


class StatusMirrorTests(Harness):
    def setUp(self):
        super().setUp()
        self.enabled[0] = False
        self.changes = []
        self.backend = FakeBackend(level_bits=0b11)
        self.trigger = self.make(on_change=lambda: self.changes.append(
            (self.trigger.status()['recording'], self.trigger.status()['latency_seconds'])))
        self.trigger.configure('gpio', 17, 27)

    def test_recording_start_and_end_notify_once_each(self):
        self.trigger.handle_edge(27, gt.EDGE_FALLING)
        self.trigger.handle_edge(27, gt.EDGE_FALLING)        # repeated edge: no change
        self.trigger.handle_edge(27, gt.EDGE_RISING)
        self.trigger.handle_edge(27, gt.EDGE_RISING)
        self.assertEqual(self.changes, [(True, None), (False, None)])

    def test_a_latency_measurement_notifies(self):
        self.trigger.note_latency(3.14159)
        self.assertEqual(self.changes, [(False, 3.14)])

    def test_releasing_a_recording_trigger_clears_the_mirror(self):
        self.trigger.handle_edge(27, gt.EDGE_FALLING)
        self.changes.clear()
        self.trigger.release()
        self.assertEqual(self.changes, [(False, None)])
        self.trigger.release()
        self.assertEqual(len(self.changes), 1)

    def test_a_failing_mirror_never_breaks_the_trigger(self):
        def boom():
            raise RuntimeError('mirror gone')
        trigger = self.make(on_change=boom)
        trigger.configure('gpio', 17, 27)
        trigger.handle_edge(27, gt.EDGE_FALLING)
        trigger.note_latency(1.0)
        self.assertTrue(trigger.status()['recording'])


class RecordingTests(Harness):
    def setUp(self):
        super().setUp()
        self.enabled[0] = False
        self.backend = FakeBackend(level_bits=0b11)      # both lines high = idle
        self.trigger = self.make()
        self.trigger.configure('gpio', 17, 27)

    def start(self):
        self.trigger.handle_edge(27, gt.EDGE_FALLING)

    def stop(self):
        self.trigger.handle_edge(27, gt.EDGE_RISING)

    def test_falling_edge_opens_a_session_and_enables_the_timelapse(self):
        self.start()
        name = timelapse.active_session(self.dir)
        self.assertTrue(name and timelapse.valid_session_name(name))
        self.assertTrue(os.path.isdir(os.path.join(self.dir, name)))
        self.assertEqual(self.enabled_calls, [True])
        status = self.trigger.status()
        self.assertTrue(status['recording'])
        self.assertEqual(status['session'], name)

    def test_frames_go_to_the_open_session(self):
        self.start()
        name = timelapse.active_session(self.dir)
        self.assertEqual(timelapse.session_frame_dir(self.dir),
                         os.path.join(self.dir, name))

    def test_rising_edge_disables_closes_and_builds_once(self):
        self.start()
        name = timelapse.active_session(self.dir)
        self.frame(name)
        self.stop()
        self.assertEqual(self.enabled_calls, [True, False])
        self.assertIsNone(timelapse.active_session(self.dir))
        self.assertEqual(self.builds, [name])
        self.assertFalse(self.trigger.status()['recording'])
        self.assertEqual(timelapse.session_frame_dir(self.dir), self.dir)

    def test_an_empty_session_is_closed_without_a_build(self):
        self.start()
        self.stop()
        self.assertEqual(self.builds, [])

    def test_repeated_start_edges_reuse_the_same_session(self):
        self.start()
        first = timelapse.active_session(self.dir)
        self.start()
        self.assertEqual(timelapse.active_session(self.dir), first)
        self.assertEqual(len(timelapse.list_sessions(self.dir)), 1)

    def test_stop_without_a_session_builds_nothing(self):
        self.stop()
        self.assertEqual(self.builds, [])
        self.assertEqual(self.enabled_calls, [False])

    def test_recording_edges_on_the_shot_pin_do_nothing(self):
        self.trigger.handle_edge(17, gt.EDGE_RISING)
        self.assertEqual(self.enabled_calls, [])

    def test_the_connect_toggle_still_works_between_edges(self):
        # The pin only sets the flag on edges; nothing here re-asserts it.
        self.start()
        self.enabled[0] = False           # user pauses via Connect/UI
        self.mono[0] += 5
        self.trigger.handle_edge(17, gt.EDGE_FALLING)
        self.assertEqual(self.shots, [])  # paused: shot ignored, session untouched
        self.assertIsNotNone(timelapse.active_session(self.dir))

    def test_arming_with_the_pin_already_active_opens_a_session(self):
        backend = FakeBackend(level_bits=0b01)           # record line low = active
        trigger = self.make(backend=backend)
        trigger.configure('gpio', 17, 27)
        self.assertIsNotNone(timelapse.active_session(self.dir))
        self.assertEqual(self.enabled_calls, [True])

    def test_arming_with_the_pin_active_resumes_the_marked_session(self):
        marked = timelapse.open_session(self.dir, now=1_700_000_000)
        backend = FakeBackend(level_bits=0b01)
        trigger = self.make(backend=backend)
        trigger.configure('gpio', 17, 27)
        self.assertEqual(timelapse.active_session(self.dir), marked)
        self.assertEqual(timelapse.list_sessions(self.dir), [marked])

    def test_arming_with_the_pin_inactive_closes_and_builds_a_stale_session(self):
        stale = timelapse.open_session(self.dir, now=1_700_000_000)
        self.frame(stale)
        backend = FakeBackend(level_bits=0b11)
        trigger = self.make(backend=backend)
        trigger.configure('gpio', 17, 27)
        self.assertIsNone(timelapse.active_session(self.dir))
        self.assertEqual(self.builds, [stale])

    def test_rearming_after_a_pin_change_keeps_the_running_session(self):
        backend = FakeBackend(level_bits=0b01)
        trigger = self.make(backend=backend)
        trigger.configure('gpio', 17, 27)
        name = timelapse.active_session(self.dir)
        trigger.configure('gpio', 22, 27)
        self.assertEqual(timelapse.active_session(self.dir), name)

    def test_a_clean_start_edge_closes_the_stale_session_of_a_cancelled_print(self):
        # Start G-code forces the pin high then low: the rising edge closes and
        # builds the stale session, the falling edge opens a fresh one.
        self.start()
        stale = timelapse.active_session(self.dir)
        self.frame(stale)
        self.stop()
        self.wall[0] += 5
        self.start()
        fresh = timelapse.active_session(self.dir)
        self.assertNotEqual(fresh, stale)
        self.assertEqual(self.builds, [stale])

    def test_status_reports_no_session_while_idle(self):
        self.assertIsNone(self.trigger.status()['session'])


if __name__ == '__main__':
    unittest.main()
