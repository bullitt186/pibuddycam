"""The test package must never touch the machine's own /data or remount ``/``."""
import os
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import app_version  # noqa: E402
import migrations  # noqa: E402
import settings_store  # noqa: E402
import updater_install  # noqa: E402


class SandboxTests(unittest.TestCase):
    def test_module_paths_point_at_the_sandbox(self):
        for value in (
            migrations.MIGRATION_STATE_PATH,
            app_version.RELEASE_STATE_PATH,
            app_version.RELEASE_METADATA_PATH,
            updater_install.DEFAULT_UPDATE_STATE_PATH,
            updater_install.DATA_ROOT,
        ):
            self.assertFalse(value.startswith('/data'), value)

    def test_data_is_never_reported_as_a_mount(self):
        self.assertFalse(settings_store.available())

    def test_default_run_pending_never_remounts_root(self):
        self.assertEqual(migrations.run_pending(), [])

    def test_late_bound_defaults_follow_the_module_paths(self):
        # Reading with no explicit path must use the (redirected) module constant.
        self.assertIsNone(updater_install.read_update_state())
        self.assertEqual(app_version.installed_release_version(), '')
        self.assertEqual(app_version.active_release_metadata()['version'], '')
        self.assertEqual(migrations._load_state(), set())

    def test_explicit_none_still_means_no_path(self):
        self.assertIsNone(updater_install.read_update_state(None))
        self.assertFalse(updater_install.write_update_state({'a': 1}, None))
        self.assertEqual(app_version.installed_release_version(None), '')


if __name__ == '__main__':
    unittest.main()
