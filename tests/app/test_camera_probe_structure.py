"""Source-structure checks for camera_probe: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_camera_probe.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))
import camera_probe  # noqa: E402


class BinaryStdoutTests(unittest.TestCase):
    """The still/JPEG captures stream a binary JPEG to stdout."""

    def test_default_runner_does_not_decode_as_text(self):
        # Guard the runner: text=True would raise on the binary capture.
        import inspect

        source = inspect.getsource(camera_probe._default_runner)
        self.assertNotIn('text=True', source)


if __name__ == '__main__':
    unittest.main()
