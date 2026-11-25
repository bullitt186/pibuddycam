"""Source-structure checks for media_library: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_media_library.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))
import timelapse  # noqa: E402


class StreamingBuildTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_stream_does_not_buffer_index_in_memory(self):
        # Structural guard: the idx1 table is written entry by entry, not
        # accumulated in a bytearray/bytes that grows with the frame count.
        import inspect

        source = inspect.getsource(timelapse._build_avi_stream)
        self.assertNotIn('bytearray', source)
        self.assertNotIn('idx +=', source)


if __name__ == '__main__':
    unittest.main()
