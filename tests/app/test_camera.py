"""capture_jpeg_after: only a live frame newer than the trigger is returned."""
import glob
import os
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import camera  # noqa: E402


class PickFreshFrameTests(unittest.TestCase):
    def test_bootstrap_frame_is_never_chosen_even_if_written_after_the_trigger(self):
        frames = [('a', 105.0), ('b', 106.0)]
        # 'a' is the cached keyframe replay: content predates the trigger.
        self.assertEqual(camera.pick_fresh_frame(frames, 100.0, complete=True), 'b')
        self.assertIsNone(camera.pick_fresh_frame(frames[:1], 100.0, complete=True))

    def test_frames_before_the_trigger_are_skipped(self):
        frames = [('a', 90.0), ('b', 95.0), ('c', 101.0), ('d', 102.0)]
        self.assertEqual(camera.pick_fresh_frame(frames, 100.5, complete=True), 'c')

    def test_the_newest_file_may_still_be_written(self):
        frames = [('a', 90.0), ('b', 101.0)]
        self.assertIsNone(camera.pick_fresh_frame(frames, 100.0, complete=False))
        frames.append(('c', 101.5))
        self.assertEqual(camera.pick_fresh_frame(frames, 100.0, complete=False), 'b')

    def test_nothing_new_yet(self):
        self.assertIsNone(camera.pick_fresh_frame([], 100.0, complete=True))
        self.assertIsNone(camera.pick_fresh_frame(
            [('a', 1.0), ('b', 2.0)], 100.0, complete=True))


class FakeProc:
    def __init__(self, pattern, script, clock):
        self.dir = os.path.dirname(pattern)
        self.script = script
        self.clock = clock
        self.terminated = False
        self.written = 0

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def tick(self):
        """Write the frames scripted up to the current fake time."""
        while self.written < len(self.script) and self.script[self.written][0] <= self.clock[0]:
            _due, data = self.script[self.written]
            path = os.path.join(self.dir, 'snap%05d.jpg' % self.written)
            with open(path, 'wb') as handle:
                handle.write(data)
            os.utime(path, (self.clock[0], self.clock[0]))
            self.written += 1


class CaptureAfterTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.procs = []

    def _run(self, script, t_trigger, **kw):
        def popen(args, **_kw):
            pattern = next(a.split('=', 1)[1] for a in args if a.startswith('location='))
            proc = FakeProc(pattern, script, self.clock)
            self.procs.append(proc)
            return proc

        def sleep(seconds):
            self.clock[0] += seconds
            for proc in self.procs:
                proc.tick()

        return camera.capture_jpeg_after(
            t_trigger, popen=popen, clock=lambda: self.clock[0], sleep=sleep, **kw)

    def frame(self, tag):
        return tag * 200

    def test_returns_the_first_live_frame_after_trigger_plus_settle(self):
        script = [
            (1000.0, self.frame(b'B')),    # bootstrap keyframe replay
            (1000.3, self.frame(b'L')),    # live, but before trigger+settle (1000.5)
            (1000.9, self.frame(b'G')),    # first good frame
            (1001.0, self.frame(b'H')),
        ]
        data = self._run(script, t_trigger=1000.0, settle=0.5)
        self.assertEqual(data[:1], b'G')
        self.assertTrue(self.procs[0].terminated)      # pipeline is stopped early

    def test_a_trigger_from_the_past_still_skips_the_bootstrap_frame(self):
        script = [(1000.0, self.frame(b'B')), (1000.2, self.frame(b'L')),
                  (1000.4, self.frame(b'M'))]
        data = self._run(script, t_trigger=900.0)
        self.assertEqual(data[:1], b'L')

    def test_times_out_when_no_fresh_frame_arrives(self):
        script = [(1000.0, self.frame(b'B'))]
        with self.assertRaises(RuntimeError):
            self._run(script, t_trigger=1000.0, timeout=1.0)
        self.assertTrue(self.procs[0].terminated)

    def test_an_empty_frame_is_an_error(self):
        script = [(1000.0, b'B' * 200), (1000.9, b'x')]
        with self.assertRaises(RuntimeError):
            self._run(script, t_trigger=1000.0)

    def test_pipeline_reads_the_local_mux_only(self):
        seen = {}

        def popen(args, **_kw):
            seen['args'] = args
            raise OSError('stop here')

        with self.assertRaises(OSError):
            camera.capture_jpeg_after(0, popen=popen)
        self.assertIn('port=8888', seen['args'])
        self.assertIn('host=127.0.0.1', seen['args'])
        self.assertIn('quality=95', seen['args'])


class CaptureJpegTests(CaptureAfterTests):
    """capture_jpeg returns as soon as the first complete frame is written."""

    def _capture(self, script, **kw):
        def popen(args, **_kw):
            pattern = next(a.split('=', 1)[1] for a in args if a.startswith('location='))
            proc = FakeProc(pattern, script, self.clock)
            self.procs.append(proc)
            return proc

        def sleep(seconds):
            self.clock[0] += seconds
            for proc in self.procs:
                proc.tick()

        return camera.capture_jpeg(
            popen=popen, clock=lambda: self.clock[0], sleep=sleep, **kw)

    def jpeg(self, tag):
        return tag * 200 + b'\xff\xd9'

    def test_returns_the_first_complete_frame_and_stops_the_pipeline(self):
        script = [(1000.2, self.jpeg(b'A')), (1000.3, self.jpeg(b'B'))]
        data = self._capture(script)
        self.assertEqual(data[:1], b'A')
        self.assertTrue(self.procs[0].terminated)
        self.assertLess(self.clock[0], 1001.0)      # not the old fixed 10 s run

    def test_a_half_written_frame_is_not_returned(self):
        script = [(1000.1, b'T' * 200), (1000.4, self.jpeg(b'C'))]
        data = self._capture(script)
        # The truncated file 0 has no EOI marker; the complete file 1 wins.
        self.assertTrue(data.endswith(b'\xff\xd9'))
        self.assertEqual(data[:1], b'C')

    def test_times_out_when_no_frame_arrives(self):
        with self.assertRaises(RuntimeError):
            self._capture([], timeout=1.0)
        self.assertTrue(self.procs[0].terminated)
        self.assertGreaterEqual(self.clock[0], 1001.0)

    def test_the_newest_sizeable_file_is_used_once_the_writer_has_stopped(self):
        # Nothing ends with the EOI marker, but the pipeline did write a frame.
        script = [(1000.1, b'Z' * 200)]
        data = self._capture(script, timeout=1.0)
        self.assertEqual(data, b'Z' * 200)

    def test_an_empty_frame_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self._capture([(1000.1, b'x')], timeout=1.0)


if __name__ == '__main__':
    unittest.main()
