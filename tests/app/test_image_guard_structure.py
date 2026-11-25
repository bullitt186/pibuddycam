"""Source-structure checks for image_guard: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_image_guard.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))
import image_guard  # noqa: E402


class ImageGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.new = os.path.join(tmp.name, 'pibuddycam')
        self.legacy = os.path.join(tmp.name, 'prusa-buddy3d-camera')

    def detected(self):
        return image_guard.legacy_image_detected(self.new, self.legacy)

    def test_entry_points_run_the_guard_before_starting(self):
        for name in ('main.py', 'admin_app.py'):
            text = (PI_DIR / name).read_text()
            main_block = text[text.index("if __name__ == '__main__':") :]
            self.assertIn('image_guard.exit_if_legacy_image()', main_block, name)


if __name__ == '__main__':
    unittest.main()
