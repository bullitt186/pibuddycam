"""WP-UI3 (AC-4/AC-5): single-owner settings mutation dispatch.

The runtime IPC handler runs on a worker thread while the coordinator is owned
by the asyncio loop. These tests pin the marshal-to-owner behavior: mutations
execute on the loop thread, serialize, and report a bounded failure on a
deadline or a closed loop -- never a fabricated success.
"""
import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

PI_DIR = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(PI_DIR))

import settings_dispatch  # noqa: E402


class LoopThread:
    """A background asyncio loop for driving the dispatcher from a test thread."""

    def __init__(self):
        self.loop = None
        self.thread = None
        self._ready = threading.Event()

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self._ready.wait(2.0)
        return self

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    def stop(self):
        if self.loop is not None:
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except RuntimeError:
                pass
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def close(self):
        if self.loop is not None:
            self.loop.close()


class RecordingCoordinator:
    """Fake coordinator that records the thread and concurrency of a mutation."""

    def __init__(self, block=None, delay=0.0):
        self.calls = []
        self.thread_ids = []
        self.block = block
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def apply_mutation(self, field, value):
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.thread_ids.append(threading.get_ident())
            self.calls.append((field, value))
            if self.block is not None:
                self.block.wait(2.0)
            if self.delay:
                time.sleep(self.delay)
            return SimpleNamespace(
                ok=True, reason=None, changed=[field], state={'camera_name': 'ok'})
        finally:
            with self._guard:
                self.active -= 1


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.loop_thread = LoopThread().start()
        self.addCleanup(self.loop_thread.stop)

    def _dispatcher(self, coordinator, timeout=5.0):
        return settings_dispatch.EventLoopMutationDispatcher(
            coordinator, self.loop_thread.loop, timeout=timeout)

    def test_mutation_runs_on_the_owning_loop_thread(self):
        coordinator = RecordingCoordinator()
        dispatcher = self._dispatcher(coordinator)
        result = dispatcher.apply('camera_name', 'Printer')
        self.assertTrue(result['ok'])
        self.assertEqual(result['settings'], {'camera_name': 'ok'})
        self.assertEqual(result['changed'], ['camera_name'])
        self.assertNotEqual(coordinator.thread_ids[0], threading.get_ident())
        self.assertEqual(coordinator.thread_ids[0], self.loop_thread.thread.ident)
        self.assertEqual(coordinator.calls, [('camera_name', 'Printer')])

    def test_concurrent_calls_serialize_on_the_loop(self):
        coordinator = RecordingCoordinator(delay=0.05)
        dispatcher = self._dispatcher(coordinator)
        threads = [
            threading.Thread(target=dispatcher.apply, args=('camera_name', str(i)))
            for i in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)
        self.assertEqual(len(coordinator.calls), 4)
        self.assertEqual(coordinator.max_active, 1, 'mutations must not overlap')

    def test_deadline_returns_a_bounded_failure(self):
        release = threading.Event()
        coordinator = RecordingCoordinator(block=release)
        dispatcher = self._dispatcher(coordinator, timeout=0.2)
        result = dispatcher.apply('camera_name', 'slow')
        self.assertFalse(result['ok'])
        self.assertIn('timed out', result['reason'])
        release.set()

    def test_closed_loop_returns_a_bounded_failure(self):
        coordinator = RecordingCoordinator()

        class ClosedLoop:
            def call_soon_threadsafe(self, *args, **kwargs):
                raise RuntimeError('Event loop is closed')

        dispatcher = settings_dispatch.EventLoopMutationDispatcher(
            coordinator, ClosedLoop())
        result = dispatcher.apply('camera_name', 'Printer')
        self.assertFalse(result['ok'])
        self.assertTrue(result['reason'])
        self.assertEqual(coordinator.calls, [])

    def test_rejects_a_missing_loop(self):
        with self.assertRaises(ValueError):
            settings_dispatch.EventLoopMutationDispatcher(RecordingCoordinator(), None)


if __name__ == '__main__':
    unittest.main()
