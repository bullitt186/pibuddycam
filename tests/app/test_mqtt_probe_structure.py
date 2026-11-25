"""Source-structure checks for mqtt_probe: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_mqtt_probe.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))


class ProbeSecretHygieneTests(unittest.TestCase):
    def test_cli_never_accepts_a_password_on_argv(self):
        # argv is world-readable (/proc/<pid>/cmdline) and shell history; the
        # manual CLI takes the password from a file or PIBUDDYCAM_MQTT_PASSWORD.
        source = (PI_DIR / 'mqtt_probe.py').read_text(encoding='utf-8')
        self.assertNotIn("add_argument('--password'", source)
        self.assertIn('--password-file', source)
        self.assertIn('PIBUDDYCAM_MQTT_PASSWORD', source)


class ProbeImportSafetyTests(unittest.TestCase):
    def test_module_import_does_not_require_paho(self):
        source = (PI_DIR / 'mqtt_probe.py').read_text(encoding='utf-8')
        # The paho import must be inside default_backend, not module scope.
        self.assertIn('import paho.mqtt.client', source)
        head = source.split('def default_backend', 1)[0]
        self.assertNotIn('import paho', head)


if __name__ == '__main__':
    unittest.main()
