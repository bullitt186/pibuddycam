"""Source-structure checks for live_monitor: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_live_monitor.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import ast
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
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]


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


class BufferSemanticsTests(unittest.TestCase):
    def test_latest_is_the_most_recent_frame_with_no_queue(self):
        counter = {'n': 0}

        def producer():
            counter['n'] += 1
            return jpeg(bytes([65 + counter['n'] % 26]) * (200 + counter['n']))

        monitor = live_monitor.LiveMonitor(
            producer=producer, interval=0.05, idle_grace=0.5, viewer_ttl=5.0
        )
        self.addCleanup(monitor.stop)
        monitor.touch('v')
        self.assertTrue(_wait_for(lambda: monitor.latest()['state'] == 'live'))
        metrics = monitor.metrics()
        latest = monitor.latest()['jpeg']
        self.assertEqual(len(latest), metrics['producer']['last_frame_bytes'])
        # One slot only: the module must not accumulate frames in a queue.
        self.assertNotIn('deque', LIVE_MONITOR_SOURCE)
        self.assertNotIn('Queue', LIVE_MONITOR_SOURCE)


if __name__ == '__main__':
    unittest.main()
