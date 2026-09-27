"""Pre-rename image guard: a new bundle must not run on an old image."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'app'
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

    def test_host_checkout_with_neither_marker_is_not_legacy(self):
        self.assertFalse(self.detected())

    def test_new_image_is_not_legacy(self):
        os.mkdir(self.new)
        self.assertFalse(self.detected())

    def test_image_with_both_markers_is_not_legacy(self):
        os.mkdir(self.new)
        os.mkdir(self.legacy)
        self.assertFalse(self.detected())

    def test_pre_rename_image_exits_with_config_status(self):
        os.mkdir(self.legacy)
        self.assertTrue(self.detected())
        codes = []
        with self.assertLogs('pibuddycam.guard', level='ERROR'):
            image_guard.exit_if_legacy_image(self.new, self.legacy, exit=codes.append)
        self.assertEqual(codes, [image_guard.LEGACY_IMAGE_EXIT_CODE])

    def test_new_image_does_not_exit(self):
        os.mkdir(self.new)
        codes = []
        image_guard.exit_if_legacy_image(self.new, self.legacy, exit=codes.append)
        self.assertEqual(codes, [])

    def test_entry_points_run_the_guard_before_starting(self):
        for name in ('main.py', 'admin_app.py'):
            text = (PI_DIR / name).read_text()
            main_block = text[text.index("if __name__ == '__main__':"):]
            self.assertIn('image_guard.exit_if_legacy_image()', main_block, name)


if __name__ == '__main__':
    unittest.main()
