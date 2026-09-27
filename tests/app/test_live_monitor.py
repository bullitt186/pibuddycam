"""WP-UI5 AC-10/AC-11: bounded shared local live-monitor producer.

Stdlib-only and hermetic: a fake producer stands in for the GStreamer capture,
time is injected where the test needs a stale frame, and no subprocess, network,
or ``/data`` path is touched. The thread-lifecycle tests use small real
timeouts (polling, no long sleeps).
"""
import ast
import json
import sys
import time
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import live_monitor  # noqa: E402

LIVE_MONITOR_SOURCE = (PI_DIR / 'live_monitor.py').read_text(encoding='utf-8')
CAMERA_SOURCE = (PI_DIR / 'camera.py').read_text(encoding='utf-8')


def jpeg(payload=b'x' * 200):
    """Return a structurally valid JPEG-shaped blob (SOI .. payload .. EOI)."""
    return b'\xff\xd8' + bytes(payload) + b'\xff\xd9'


class _MutableClock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


def _wait_for(predicate, timeout=2.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _executable_strings(source):
    """Return non-docstring string constants from ``source`` (AST-based).

    Prose comments and docstrings legitimately explain *why* libcamera is not
    used; only executable string literals can invoke a process.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value not in docstrings
    ]


class ValidJpegTests(unittest.TestCase):
    def test_accepts_complete_bounded_jpeg(self):
        self.assertTrue(live_monitor.valid_jpeg(jpeg()))

    def test_rejects_short_partial_and_wrong_markers(self):
        cases = (
            b'',
            b'\xff\xd8' + b'\x00' * 4,
            jpeg()[:-2],                       # missing EOI
            b'\x00\x00' + b'x' * 200 + b'\xff\xd9',
            jpeg() + b'trailing',
            b'not a jpeg at all' * 20,
            None,
            123,
        )
        for case in cases:
            with self.subTest(case=type(case).__name__):
                self.assertFalse(live_monitor.valid_jpeg(case))

    def test_rejects_over_max_bytes(self):
        data = jpeg(b'x' * 500)
        self.assertFalse(live_monitor.valid_jpeg(data, max_bytes=100))
        self.assertTrue(live_monitor.valid_jpeg(data, max_bytes=len(data)))


class NoLibcameraTests(unittest.TestCase):
    def test_camera_producer_reads_the_mux_and_never_invokes_rpicam(self):
        self.assertIn('8888', CAMERA_SOURCE)
        self.assertIn('tcpclientsrc', CAMERA_SOURCE)
        for value in _executable_strings(CAMERA_SOURCE):
            self.assertNotIn('rpicam', value.lower(), value)

    def test_live_monitor_default_producer_reuses_the_mux_capture(self):
        for value in _executable_strings(LIVE_MONITOR_SOURCE):
            self.assertNotIn('rpicam', value.lower(), value)
        self.assertIn('from camera import capture_jpeg', LIVE_MONITOR_SOURCE)
        self.assertIn('SOURCE_LABEL', LIVE_MONITOR_SOURCE)


class ProducerSharingTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.monitor = live_monitor.LiveMonitor(
            producer=self._producer,
            interval=0.05,
            idle_grace=0.5,
            viewer_ttl=5.0,
            max_viewers=4,
            fresh_seconds=5.0,
            max_stale_seconds=30.0,
        )
        self.addCleanup(self.monitor.stop)

    def _producer(self):
        self.calls.append(time.monotonic())
        return jpeg()

    def test_one_producer_thread_serves_multiple_viewers(self):
        self.assertTrue(self.monitor.touch('viewer-a'))
        self.assertTrue(self.monitor.touch('viewer-b'))
        self.assertTrue(_wait_for(lambda: self.monitor.latest()['state'] == 'live'))
        thread = self.monitor._thread
        self.assertIsNotNone(thread)
        self.assertTrue(thread.is_alive())
        # A third viewer reuses the same producer thread; no second thread and no
        # per-viewer decoder is started.
        self.assertTrue(self.monitor.touch('viewer-c'))
        self.assertIs(self.monitor._thread, thread)
        self.assertEqual(self.monitor.metrics()['viewers']['active'], 3)
        # All viewers read the same one-slot frame; the producer call count is
        # paced by the interval, not multiplied by viewers.
        frame = self.monitor.latest()['jpeg']
        self.assertTrue(frame)
        time.sleep(0.05)
        self.assertLess(len(self.calls), 30)

    def test_viewer_cap_rejects_new_viewer_but_refreshes_existing(self):
        monitor = live_monitor.LiveMonitor(
            producer=lambda: jpeg(), interval=0.05, idle_grace=0.5,
            max_viewers=1, viewer_ttl=5.0,
        )
        self.addCleanup(monitor.stop)
        self.assertTrue(monitor.touch('a'))
        self.assertFalse(monitor.touch('b'))
        self.assertTrue(monitor.touch('a'))
        self.assertEqual(monitor.metrics()['viewers']['active'], 1)

    def test_empty_viewer_id_is_rejected(self):
        self.assertFalse(self.monitor.touch(''))
        self.assertFalse(self.monitor.touch(None))


class BufferSemanticsTests(unittest.TestCase):
    def test_latest_is_the_most_recent_frame_with_no_queue(self):
        counter = {'n': 0}

        def producer():
            counter['n'] += 1
            return jpeg(bytes([65 + counter['n'] % 26]) * (200 + counter['n']))

        monitor = live_monitor.LiveMonitor(
            producer=producer, interval=0.05, idle_grace=0.5, viewer_ttl=5.0)
        self.addCleanup(monitor.stop)
        monitor.touch('v')
        self.assertTrue(_wait_for(lambda: monitor.latest()['state'] == 'live'))
        metrics = monitor.metrics()
        latest = monitor.latest()['jpeg']
        self.assertEqual(len(latest), metrics['producer']['last_frame_bytes'])
        # One slot only: the module must not accumulate frames in a queue.
        self.assertNotIn('deque', LIVE_MONITOR_SOURCE)
        self.assertNotIn('Queue', LIVE_MONITOR_SOURCE)

    def test_malformed_and_oversized_upstream_never_served(self):
        responses = [b'not-a-jpeg', jpeg(b'x' * 5000)]
        monitor = live_monitor.LiveMonitor(
            producer=lambda: responses.pop(0) if responses else b'',
            interval=0.05, idle_grace=0.5, viewer_ttl=5.0,
            max_frame_bytes=1024,
        )
        self.addCleanup(monitor.stop)
        monitor.touch('v')
        self.assertTrue(_wait_for(
            lambda: monitor.metrics()['producer']['invalid_frames'] >= 2))
        self.assertEqual(monitor.latest()['state'], 'unavailable')
        self.assertIsNone(monitor.latest()['jpeg'])
        self.assertEqual(monitor.metrics()['producer']['frames_produced'], 0)

    def test_failed_capture_is_counted_and_never_raises(self):
        def producer():
            raise RuntimeError('gst failed')

        monitor = live_monitor.LiveMonitor(
            producer=producer, interval=0.05, idle_grace=0.5, viewer_ttl=5.0)
        self.addCleanup(monitor.stop)
        monitor.touch('v')
        self.assertTrue(_wait_for(
            lambda: monitor.metrics()['producer']['captures_failed'] >= 1))
        self.assertEqual(monitor.latest()['state'], 'unavailable')

    def test_stale_frame_beyond_bound_is_not_served(self):
        clock = _MutableClock()
        monitor = live_monitor.LiveMonitor(
            producer=lambda: jpeg(), interval=0.05, idle_grace=0.5,
            viewer_ttl=1000.0, fresh_seconds=1.0, max_stale_seconds=2.0,
            clock=clock,
        )
        self.addCleanup(monitor.stop)
        monitor.touch('v')
        self.assertTrue(_wait_for(lambda: monitor.latest()['state'] == 'live'))
        monitor.stop()
        clock.advance(3.0)
        frame = monitor.latest()
        self.assertEqual(frame['state'], 'stale')
        self.assertIsNone(frame['jpeg'])
        self.assertGreater(frame['age_seconds'], 2.0)


class LifecycleTests(unittest.TestCase):
    def test_idle_teardown_after_last_lease_expires(self):
        monitor = live_monitor.LiveMonitor(
            producer=lambda: jpeg(), interval=0.05, idle_grace=0.1,
            viewer_ttl=0.05,
        )
        self.addCleanup(monitor.stop)
        self.assertTrue(monitor.touch('v'))
        self.assertTrue(_wait_for(lambda: monitor.running))
        self.assertTrue(_wait_for(lambda: not monitor.running, timeout=2.0))
        self.assertFalse(monitor._thread is not None and monitor._thread.is_alive())

    def test_stop_is_clean_and_restartable(self):
        monitor = live_monitor.LiveMonitor(
            producer=lambda: jpeg(), interval=0.05, idle_grace=5.0, viewer_ttl=5.0)
        self.addCleanup(monitor.stop)
        monitor.touch('v')
        self.assertTrue(_wait_for(lambda: monitor.running))
        monitor.stop()
        self.assertFalse(monitor.running)
        self.assertTrue(monitor.touch('v'))
        self.assertTrue(_wait_for(lambda: monitor.running))

    def test_release_drops_the_lease(self):
        monitor = live_monitor.LiveMonitor(
            producer=lambda: jpeg(), interval=0.05, idle_grace=5.0, viewer_ttl=5.0)
        self.addCleanup(monitor.stop)
        monitor.touch('v')
        self.assertEqual(monitor.viewer_count(), 1)
        monitor.release('v')
        self.assertEqual(monitor.viewer_count(), 0)


class CadenceAndBackoffTests(unittest.TestCase):
    def test_faster_than_1fps_is_rejected(self):
        with self.assertRaises(ValueError):
            live_monitor.LiveMonitor(producer=lambda: jpeg(), interval=1.01)
        with self.assertRaises(ValueError):
            live_monitor.LiveMonitor(producer=lambda: jpeg(), interval=30.0)

    def test_default_interval_is_at_most_1fps(self):
        self.assertLessEqual(live_monitor.DEFAULT_INTERVAL_SECONDS, 1.0)
        monitor = live_monitor.LiveMonitor(producer=lambda: jpeg())
        self.addCleanup(monitor.stop)
        self.assertLessEqual(monitor.metrics()['interval_seconds'], 1.0)

    def test_failure_backoff_grows_and_is_capped(self):
        monitor = live_monitor.LiveMonitor(
            producer=lambda: jpeg(), interval=0.5, idle_grace=1.0,
            failure_backoff_max=2.0)
        self.addCleanup(monitor.stop)
        first = monitor._backoff_delay(False)
        second = monitor._backoff_delay(False)
        self.assertGreaterEqual(first, 0.5)
        self.assertGreater(second, first)
        for _ in range(10):
            capped = monitor._backoff_delay(False)
        self.assertLessEqual(capped, 2.0)
        self.assertEqual(monitor._backoff_delay(True), 0.5)

    def test_no_capture_without_an_active_lease(self):
        calls = []
        monitor = live_monitor.LiveMonitor(
            producer=lambda: (calls.append(1), jpeg())[1],
            interval=0.05, idle_grace=0.1, viewer_ttl=5.0)
        self.addCleanup(monitor.stop)
        monitor.start()
        time.sleep(0.2)
        self.assertEqual(calls, [])
        self.assertEqual(monitor.metrics()['producer']['frames_produced'], 0)


class LeaseSemanticsTests(unittest.TestCase):
    def test_repeated_polling_by_one_viewer_does_not_exhaust_the_cap(self):
        monitor = live_monitor.LiveMonitor(
            producer=lambda: jpeg(), interval=0.05, idle_grace=5.0,
            viewer_ttl=5.0, max_viewers=1)
        self.addCleanup(monitor.stop)
        for _ in range(20):
            self.assertTrue(monitor.touch('same-session'))
        self.assertEqual(monitor.viewer_count(), 1)

    def test_many_distinct_viewers_cannot_grow_lease_state(self):
        monitor = live_monitor.LiveMonitor(
            producer=lambda: jpeg(), interval=0.05, idle_grace=5.0,
            viewer_ttl=5.0, max_viewers=3)
        self.addCleanup(monitor.stop)
        accepted = [monitor.touch(f'viewer-{index}') for index in range(50)]
        self.assertEqual(sum(1 for ok in accepted if ok), 3)
        self.assertEqual(monitor.viewer_count(), 3)

    def test_expired_lease_frees_a_cap_slot(self):
        clock = _MutableClock()
        monitor = live_monitor.LiveMonitor(
            producer=lambda: jpeg(), interval=0.05, idle_grace=5.0,
            viewer_ttl=1.0, max_viewers=1, clock=clock)
        self.addCleanup(monitor.stop)
        self.assertTrue(monitor.touch('first'))
        self.assertFalse(monitor.touch('second'))
        clock.advance(2.0)
        self.assertTrue(monitor.touch('second'))
        self.assertEqual(monitor.viewer_count(), 1)

    def test_release_frees_a_cap_slot_immediately(self):
        monitor = live_monitor.LiveMonitor(
            producer=lambda: jpeg(), interval=0.05, idle_grace=5.0,
            viewer_ttl=5.0, max_viewers=1)
        self.addCleanup(monitor.stop)
        self.assertTrue(monitor.touch('first'))
        self.assertFalse(monitor.touch('second'))
        monitor.release('first')
        self.assertTrue(monitor.touch('second'))


class MetricsTests(unittest.TestCase):
    def test_metrics_are_bounded_and_secret_free(self):
        monitor = live_monitor.LiveMonitor(
            producer=lambda: jpeg(), interval=0.05, idle_grace=5.0, viewer_ttl=5.0)
        self.addCleanup(monitor.stop)
        monitor.touch('v')
        self.assertTrue(_wait_for(lambda: monitor.latest()['state'] == 'live'))
        metrics = monitor.metrics()
        blob = json.dumps(metrics, sort_keys=True)
        for canary in (
            '/data', '/run', '/opt', 'token', 'password', 'secret',
            'fingerprint', 'rpicam', 'ssid',
        ):
            self.assertNotIn(canary, blob, canary)
        self.assertEqual(metrics['source'], live_monitor.SOURCE_LABEL)
        self.assertEqual(metrics['frame']['state'], 'live')
        self.assertGreaterEqual(metrics['producer']['frames_produced'], 1)
        self.assertGreater(metrics['producer']['last_frame_bytes'], 0)
        self.assertEqual(metrics['viewers']['cap'], 4)


if __name__ == '__main__':
    unittest.main()
