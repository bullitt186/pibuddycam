"""Source-structure checks for camera_source: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_camera_source.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))


class UnitTests(unittest.TestCase):
    def test_unit_reads_rotation_env_and_execs_camera_source(self):
        unit = (PI_DIR / 'systemd' / 'rpicam-source.service').read_text()
        self.assertIn('Environment=CAM_WIDTH=1920 CAM_HEIGHT=1080 CAM_ROTATION=0', unit)
        self.assertIn('EnvironmentFile=-/etc/pibuddycam/rotation.env', unit)
        self.assertIn('ExecStart=/opt/pibuddycam/launcher.sh camera_source.py', unit)
        self.assertNotIn('--rotation 180', unit)
        # The rotation file is read after the quality files, independent of them.
        self.assertLess(unit.index('quality.live.env'), unit.index('rotation.env\n'))


if __name__ == '__main__':
    unittest.main()
