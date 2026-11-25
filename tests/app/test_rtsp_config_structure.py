"""Source-structure checks for rtsp_config: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_rtsp_config.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))


class HomeAssistantUnitTests(unittest.TestCase):
    def test_unit_is_independent_and_always_on(self):
        unit = (PI_DIR / 'systemd' / 'pibuddycam-ha-rtsp.service').read_text()
        self.assertIn('RTSP_PORT=8555', unit)
        self.assertIn('RTSP_PATH=/live', unit)
        self.assertIn('Requires=rpicam-source.service', unit)
        self.assertIn('WantedBy=multi-user.target', unit)
        self.assertNotIn('rtsp.mode', unit)


if __name__ == '__main__':
    unittest.main()
