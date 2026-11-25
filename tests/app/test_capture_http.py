import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import camera  # noqa: E402  (stdlib-only: subprocess/tempfile/os/glob)


class SnapshotJpegQualityTests(unittest.TestCase):
    """GAP-SNAPSHOT-03: the capture pipeline encodes JPEG at quality 95."""

    def test_capture_pipeline_uses_jpegenc_quality_95(self):
        captured = {}

        class _Proc:
            def poll(self):
                return 0

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        def fake_popen(args, **kwargs):
            captured['args'] = list(args)
            location = next(a.split('location=', 1)[1] for a in args if a.startswith('location='))
            with open(location % 0, 'wb') as f:
                f.write(b'\xff' * 200 + b'\xd9')
            return _Proc()

        data = camera.capture_jpeg(popen=fake_popen)

        self.assertEqual(len(data), 201)
        args = captured['args']
        self.assertIn('jpegenc', args)
        quality_index = args.index('jpegenc') + 1
        self.assertEqual(args[quality_index], 'quality=95')


if __name__ == '__main__':
    unittest.main()
