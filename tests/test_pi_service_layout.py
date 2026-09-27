"""AC-1/AC-2/AC-7: dedicated service account, durable layout, DATA gate.

Reads the unit templates and exercises persist_restore's layout creation with
mocks; nothing here touches the real ``/data`` or creates system accounts.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import persist_restore  # noqa: E402

APP_UNITS = (
    'rpicam-source.service',
    'pibuddycam.service',
    'pibuddycam-rtsp.service',
    'pibuddycam-ha-rtsp.service',
)
ALL_UNITS = APP_UNITS + (
    'bootlog.service',
    'pi-persist.service',
    'pibuddycam-data-ready.service',
    'data-ready.target',
)
# Files this contract is allowed to touch that must not name a personal account
# or a home-directory install path.
SERVICE_FILES = (
    'persist_restore.py',
    'quality.py',
    'data_ready.py',
    'deploy.sh',
    'bootstrap.sh',
) + tuple('systemd/' + name for name in ALL_UNITS)


def unit(name):
    return (PI_DIR / 'systemd' / name).read_text()


class ServiceIdentityConstantsTests(unittest.TestCase):
    def test_default_service_user_is_dedicated(self):
        self.assertEqual(persist_restore.DEFAULT_SERVICE_USER, 'pibuddycam')

    def test_durable_layout_constants(self):
        self.assertEqual(persist_restore.DATA_CONFIG_DIR, '/data/pibuddycam/config')
        self.assertEqual(persist_restore.DATA_RELEASES_DIR, '/data/pibuddycam/releases')
        self.assertEqual(persist_restore.DATA_BACKUPS_DIR, '/data/pibuddycam/backups')
        self.assertEqual(persist_restore.DATA_NETWORK_DIR, '/data/network')
        self.assertEqual(
            persist_restore.DATA_NETWORK_CONNECTIONS,
            '/data/network/system-connections',
        )

    def test_layout_covers_all_durable_dirs_with_safe_modes(self):
        modes = dict(persist_restore.DATA_LAYOUT)
        for path in (
            '/data/sdcard',
            '/data/sdcard/timelapse',
            '/data/pibuddycam',
            '/data/pibuddycam/config',
            '/data/pibuddycam/releases',
            '/data/pibuddycam/backups',
            '/data/network',
            '/data/network/system-connections',
        ):
            self.assertIn(path, modes)
        # config/backups hold secrets and migration backups: not world-readable.
        self.assertEqual(modes['/data/pibuddycam/config'], 0o750)
        self.assertEqual(modes['/data/pibuddycam/backups'], 0o750)
        # The root updater owns installation targets; releases stays traversable.
        self.assertEqual(modes['/data/pibuddycam/releases'], 0o755)


class DurableLayoutCreationTests(unittest.TestCase):
    def test_main_creates_and_chowns_every_dir(self):
        calls = {'makedirs': [], 'chmod': [], 'chown': []}
        with patch.object(persist_restore.settings_store, 'available', return_value=True), \
                patch.object(persist_restore.migrations, 'run_pending', return_value=[]), \
                patch.object(persist_restore.os, 'makedirs',
                             side_effect=lambda p, exist_ok=False: calls['makedirs'].append(p)), \
                patch.object(persist_restore.os, 'chmod',
                             side_effect=lambda p, m: calls['chmod'].append((p, m))), \
                patch.object(persist_restore, '_chown',
                             side_effect=lambda p, u: calls['chown'].append((p, u))), \
                patch.object(persist_restore, '_bind_mount'), \
                patch.object(persist_restore, '_provision_admin_tls'), \
                patch.object(persist_restore, '_restore_settings'), \
                patch.object(persist_restore, '_prune_timelapse'):
            self.assertEqual(persist_restore.main(), 0)

        expected = [path for path, _mode in persist_restore.DATA_LAYOUT]
        self.assertEqual(calls['makedirs'], expected)
        # Root-only entries (the NM keyfile store and the releases tree) are
        # re-asserted as root-owned; everything else is handed to the service
        # account so it can write state/config/backups/frames.
        expected_chown = [
            (path, 'root' if path in persist_restore.ROOT_ONLY_DIRS else 'pibuddycam')
            for path in expected
        ]
        self.assertEqual(calls['chown'], expected_chown)
        chmod = dict(calls['chmod'])
        self.assertEqual(chmod['/data/pibuddycam/config'], 0o750)
        self.assertEqual(chmod['/data/pibuddycam/backups'], 0o750)
        self.assertEqual(chmod['/data/pibuddycam/releases'], 0o755)
        self.assertEqual(chmod['/data/network'], 0o700)
        self.assertEqual(chmod['/data/network/system-connections'], 0o700)

    def test_root_only_dirs_are_owned_by_root(self):
        self.assertEqual(
            persist_restore.ROOT_ONLY_DIRS,
            {
                '/data/network',
                '/data/network/system-connections',
                '/data/pibuddycam/releases',
            },
        )

    def test_service_user_env_override(self):
        users = []
        with patch.object(persist_restore.settings_store, 'available', return_value=True), \
                patch.object(persist_restore.migrations, 'run_pending', return_value=[]), \
                patch.object(persist_restore.os, 'makedirs'), \
                patch.object(persist_restore.os, 'chmod'), \
                patch.object(persist_restore, '_chown',
                             side_effect=lambda p, u: users.append((p, u))), \
                patch.object(persist_restore, '_bind_mount'), \
                patch.object(persist_restore, '_provision_admin_tls'), \
                patch.object(persist_restore, '_restore_settings'), \
                patch.object(persist_restore, '_prune_timelapse'), \
                patch.dict(os.environ, {'SERVICE_USER': 'custom-svc'}):
            persist_restore.main()
        self.assertTrue(users)
        for path, user in users:
            if path in persist_restore.ROOT_ONLY_DIRS:
                self.assertEqual(user, 'root', path)
            else:
                self.assertEqual(user, 'custom-svc', path)

    def test_main_inert_when_data_unavailable(self):
        with patch.object(persist_restore.settings_store, 'available', return_value=False), \
                patch.object(persist_restore.os, 'makedirs',
                             side_effect=AssertionError('must not create dirs')):
            self.assertEqual(persist_restore.main(), 0)


class UnitLayoutTests(unittest.TestCase):
    def test_app_units_use_dedicated_account_and_app_root(self):
        for name in APP_UNITS:
            with self.subTest(unit=name):
                text = unit(name)
                self.assertIn('User=pibuddycam', text)
                self.assertIn('/opt/pibuddycam', text)
                self.assertNotIn('/home/', text)

    def test_app_units_are_gated_on_data_ready(self):
        for name in APP_UNITS:
            with self.subTest(unit=name):
                text = unit(name)
                after = [ln for ln in text.splitlines() if ln.startswith('After=')]
                requires = [ln for ln in text.splitlines() if ln.startswith('Requires=')]
                self.assertTrue(
                    any('data-ready.target' in ln for ln in after), text)
                self.assertTrue(
                    any('data-ready.target' in ln for ln in requires), text)

    def test_ha_unit_still_requires_rpicam_source(self):
        self.assertIn('Requires=rpicam-source.service', unit('pibuddycam-ha-rtsp.service'))

    def test_cam_unit_creates_the_service_owned_runtime_directory(self):
        # WP-UI2/AC-4: the local control socket lives under /run/pibuddycam; /run
        # is root-owned, so systemd must create the directory for the service.
        text = unit('pibuddycam.service')
        self.assertIn('RuntimeDirectory=pibuddycam', text)
        self.assertIn('RuntimeDirectoryMode=0750', text)

    def test_bootlog_and_persist_use_app_root(self):
        self.assertIn('ExecStart=/opt/pibuddycam/bootlog.sh', unit('bootlog.service'))
        persist = unit('pi-persist.service')
        self.assertIn('Environment=SERVICE_USER=pibuddycam', persist)
        self.assertIn('launcher.sh persist_restore.py', persist)
        self.assertIn('RequiresMountsFor=/data', persist)
        self.assertIn('ConditionPathIsMountPoint=/data', persist)

    def test_pi_persist_runs_before_gate_and_services(self):
        before = [ln for ln in unit('pi-persist.service').splitlines()
                  if ln.startswith('Before=')][0]
        for name in ('pibuddycam-data-ready.service', 'data-ready.target') + APP_UNITS:
            with self.subTest(name=name):
                self.assertIn(name, before)

    def test_pi_persist_provisions_admin_tls_before_admin(self):
        # Appliance image/security defect: pi-persist writes /etc/pibuddycam/
        # admin.env, so it must be ordered before pibuddycam-admin.service.
        before = [ln for ln in unit('pi-persist.service').splitlines()
                  if ln.startswith('Before=')][0]
        self.assertIn('pibuddycam-admin.service', before)

    def test_data_ready_gate_units(self):
        service = unit('pibuddycam-data-ready.service')
        self.assertIn('Type=oneshot', service)
        self.assertIn('RemainAfterExit=yes', service)
        self.assertIn('RequiresMountsFor=/data', service)
        self.assertIn('After=local-fs.target pi-persist.service', service)
        self.assertIn('Before=data-ready.target', service)
        self.assertIn('/opt/pibuddycam/data_ready.py', service)
        self.assertIn('WantedBy=data-ready.target', service)

        target = unit('data-ready.target')
        self.assertIn('Requires=pibuddycam-data-ready.service', target)
        self.assertIn('After=pibuddycam-data-ready.service', target)
        self.assertIn('WantedBy=multi-user.target', target)

    def test_no_personal_username_or_home_path(self):
        for rel in SERVICE_FILES:
            with self.subTest(file=rel):
                text = (PI_DIR / rel).read_text()
                self.assertNotIn('bullitt', text)
                # Word boundaries: the pibuddycam account legitimately starts with "pi".
                self.assertNotRegex(text, r'/home/pi\b')
                self.assertNotRegex(text, r'User=pi\b')


if __name__ == '__main__':
    unittest.main()
