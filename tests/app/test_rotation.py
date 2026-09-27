"""Rotation env file and live-apply contract (camera image rotation setting)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import rotation  # noqa: E402


class RotationEnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'rotation.env')

    def test_round_trip_every_rotation(self):
        for degrees in rotation.ROTATIONS:
            with self.subTest(degrees=degrees):
                self.assertEqual(rotation.write_current(degrees, self.path), degrees)
                self.assertEqual(Path(self.path).read_text(), f'CAM_ROTATION={degrees}\n')
                self.assertEqual(rotation.read_current(self.path), degrees)

    def test_missing_file_is_default(self):
        self.assertEqual(rotation.read_current(self.path), rotation.DEFAULT_ROTATION)
        self.assertEqual(rotation.DEFAULT_ROTATION, 0)

    def test_invalid_contents_are_default(self):
        for text in ('CAM_ROTATION=45\n', 'CAM_ROTATION=abc\n', 'OTHER=90\n', ''):
            with self.subTest(text=text):
                Path(self.path).write_text(text)
                self.assertEqual(rotation.read_current(self.path), 0)

    def test_invalid_write_falls_back_to_default(self):
        self.assertEqual(rotation.write_current(33, self.path), 0)
        self.assertEqual(rotation.read_current(self.path), 0)

    def test_validation_and_orientation(self):
        self.assertEqual(rotation.valid_rotation(270), 270)
        for bad in (True, 90.0, '90', 45, None):
            self.assertIsNone(rotation.valid_rotation(bad), bad)
        self.assertEqual(rotation.oriented(1920, 1080, 0), (1920, 1080))
        self.assertEqual(rotation.oriented(1920, 1080, 180), (1920, 1080))
        self.assertEqual(rotation.oriented(1920, 1080, 90), (1080, 1920))
        self.assertEqual(rotation.oriented(1920, 1080, 270), (1080, 1920))


class RotationApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'rotation.env')

    def test_success_writes_and_restarts(self):
        calls = []
        self.assertTrue(rotation.apply(90, lambda: calls.append(1) or 0, self.path))
        self.assertEqual(calls, [1])
        self.assertEqual(rotation.read_current(self.path), 90)

    def test_failed_restart_restores_previous_file(self):
        rotation.write_current(180, self.path)
        self.assertFalse(rotation.apply(90, lambda: 1, self.path))
        self.assertEqual(rotation.read_current(self.path), 180)

    def test_failed_restart_removes_file_that_did_not_exist(self):
        def boom():
            raise OSError('helper missing')
        self.assertFalse(rotation.apply(270, boom, self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_invalid_rotation_never_restarts(self):
        calls = []
        self.assertFalse(rotation.apply(45, lambda: calls.append(1) or 0, self.path))
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(self.path))


if __name__ == '__main__':
    unittest.main()
