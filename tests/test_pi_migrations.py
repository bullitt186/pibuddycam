"""Boot-time ROOT migration tests (migrations.py).

Each migration is tested for idempotency (applying twice is safe) and
correctness.  The runner is tested with a fake ROOT and state file.
"""
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import migrations


class CreateMntSdcardTests(unittest.TestCase):
    def test_creates_directory(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertTrue(migrations._001_create_mnt_sdcard(root))
            self.assertTrue(os.path.isdir(os.path.join(root, 'mnt', 'sdcard')))

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            migrations._001_create_mnt_sdcard(root)
            self.assertTrue(migrations._001_create_mnt_sdcard(root))


class InstallSambaConfigTests(unittest.TestCase):
    def test_creates_share_config(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'etc', 'samba'))
            Path(os.path.join(root, 'etc', 'samba', 'smb.conf')).write_text(
                '[global]\n   workgroup = WORKGROUP\n'
            )
            self.assertTrue(migrations._002_install_samba_config(root))
            conf = Path(os.path.join(root, 'etc', 'samba', 'smb-sdcard.conf')).read_text()
            self.assertIn('path = /mnt/sdcard', conf)
            self.assertIn('force user = prusa-cam', conf)

    def test_appends_include_to_existing_smb_conf(self):
        with tempfile.TemporaryDirectory() as root:
            samba = os.path.join(root, 'etc', 'samba')
            os.makedirs(samba)
            Path(os.path.join(samba, 'smb.conf')).write_text('[global]\n')
            migrations._002_install_samba_config(root)
            content = Path(os.path.join(samba, 'smb.conf')).read_text()
            self.assertIn('include = /etc/samba/smb-sdcard.conf', content)

    def test_does_not_duplicate_include(self):
        with tempfile.TemporaryDirectory() as root:
            samba = os.path.join(root, 'etc', 'samba')
            os.makedirs(samba)
            Path(os.path.join(samba, 'smb.conf')).write_text(
                '[global]\n\ninclude = /etc/samba/smb-sdcard.conf\n'
            )
            migrations._002_install_samba_config(root)
            content = Path(os.path.join(samba, 'smb.conf')).read_text()
            self.assertEqual(content.count('include = /etc/samba/smb-sdcard.conf'), 1)

    def test_creates_tmpfiles(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'etc', 'samba'))
            Path(os.path.join(root, 'etc', 'samba', 'smb.conf')).write_text('[global]\n')
            migrations._002_install_samba_config(root)
            tmpf = Path(os.path.join(root, 'etc', 'tmpfiles.d', 'buddy3d-samba.conf'))
            self.assertTrue(tmpf.exists())
            self.assertIn('/var/lib/samba/private', tmpf.read_text())

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'etc', 'samba'))
            Path(os.path.join(root, 'etc', 'samba', 'smb.conf')).write_text('[global]\n')
            migrations._002_install_samba_config(root)
            self.assertTrue(migrations._002_install_samba_config(root))


class MaskConsoleSetupTests(unittest.TestCase):
    def test_creates_mask_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            systemd = os.path.join(root, 'etc', 'systemd', 'system')
            os.makedirs(systemd)
            self.assertTrue(migrations._003_mask_console_setup(root))
            link = os.path.join(systemd, 'console-setup.service')
            self.assertTrue(os.path.islink(link))
            self.assertEqual(os.readlink(link), '/dev/null')

    def test_replaces_existing_unit_file(self):
        with tempfile.TemporaryDirectory() as root:
            systemd = os.path.join(root, 'etc', 'systemd', 'system')
            os.makedirs(systemd)
            unit = os.path.join(systemd, 'console-setup.service')
            Path(unit).write_text('[Unit]\nDescription=test\n')
            self.assertTrue(migrations._003_mask_console_setup(root))
            self.assertTrue(os.path.islink(unit))
            self.assertEqual(os.readlink(unit), '/dev/null')

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            systemd = os.path.join(root, 'etc', 'systemd', 'system')
            os.makedirs(systemd)
            migrations._003_mask_console_setup(root)
            self.assertTrue(migrations._003_mask_console_setup(root))


class PrusaPrivNoBlockTests(unittest.TestCase):
    ORIGINAL = textwrap.dedent("""\
        #!/bin/bash
        case "$verb" in
           start-camera)
              exec "$SYSTEMCTL" start prusa-camera.target
              ;;
        esac
    """)

    FIXED = textwrap.dedent("""\
        #!/bin/bash
        case "$verb" in
           start-camera)
              exec "$SYSTEMCTL" --no-block start prusa-camera.target
              ;;
        esac
    """)

    def test_patches_blocking_start(self):
        with tempfile.TemporaryDirectory() as root:
            helper = os.path.join(root, 'usr', 'libexec', 'prusa-cam', 'prusa-priv')
            os.makedirs(os.path.dirname(helper))
            Path(helper).write_text(self.ORIGINAL)
            self.assertTrue(migrations._005_prusa_priv_no_block(root))
            self.assertIn('--no-block', Path(helper).read_text())

    def test_already_patched(self):
        with tempfile.TemporaryDirectory() as root:
            helper = os.path.join(root, 'usr', 'libexec', 'prusa-cam', 'prusa-priv')
            os.makedirs(os.path.dirname(helper))
            Path(helper).write_text(self.FIXED)
            self.assertTrue(migrations._005_prusa_priv_no_block(root))

    def test_missing_helper_returns_false(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertFalse(migrations._005_prusa_priv_no_block(root))

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            helper = os.path.join(root, 'usr', 'libexec', 'prusa-cam', 'prusa-priv')
            os.makedirs(os.path.dirname(helper))
            Path(helper).write_text(self.ORIGINAL)
            migrations._005_prusa_priv_no_block(root)
            migrations._005_prusa_priv_no_block(root)
            content = Path(helper).read_text()
            self.assertEqual(content.count('--no-block'), 1)


class StateTests(unittest.TestCase):
    def test_empty_state_returns_empty_set(self):
        self.assertEqual(migrations._load_state('/nonexistent'), set())

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'state.json')
            applied = {'001_create_mnt_sdcard', '002_install_samba_config'}
            migrations._save_state(applied, path)
            self.assertEqual(migrations._load_state(path), applied)


class RunPendingTests(unittest.TestCase):
    def test_applies_all_on_fresh_state(self):
        with tempfile.TemporaryDirectory() as root:
            state = os.path.join(root, 'migrations.json')
            # Create the files the migrations expect
            helper_dir = os.path.join(root, 'usr', 'libexec', 'prusa-cam')
            os.makedirs(helper_dir)
            Path(os.path.join(helper_dir, 'prusa-priv')).write_text(
                '#!/bin/bash\nexec "$SYSTEMCTL" start prusa-camera.target\n'
            )
            samba_dir = os.path.join(root, 'etc', 'samba')
            os.makedirs(samba_dir)
            Path(os.path.join(samba_dir, 'smb.conf')).write_text('[global]\n')
            systemd_dir = os.path.join(root, 'etc', 'systemd', 'system')
            os.makedirs(systemd_dir)
            Path(os.path.join(systemd_dir, 'pi-persist.service')).write_text(
                '[Service]\n'
                'ExecStart=/opt/prusa-cam/venv/bin/python /opt/prusa-cam/persist_restore.py\n'
            )

            # Patch out remount and daemon-reload (test runs unprivileged)
            original_remount = migrations._remount
            original_run = migrations.subprocess.run
            migrations._remount = lambda mode: True
            migrations.subprocess.run = lambda *a, **k: None
            try:
                applied = migrations.run_pending(root=root, state_path=state)
            finally:
                migrations._remount = original_remount
                migrations.subprocess.run = original_run

            self.assertEqual(len(applied), len(migrations.MIGRATIONS))
            # State file records them
            recorded = migrations._load_state(state)
            for name, _ in migrations.MIGRATIONS:
                self.assertIn(name, recorded)

    def test_skips_already_applied(self):
        with tempfile.TemporaryDirectory() as root:
            state = os.path.join(root, 'migrations.json')
            # Pre-record all as applied
            all_names = {name for name, _ in migrations.MIGRATIONS}
            migrations._save_state(all_names, state)

            original_remount = migrations._remount
            migrations._remount = lambda mode: True
            try:
                applied = migrations.run_pending(root=root, state_path=state)
            finally:
                migrations._remount = original_remount

            self.assertEqual(applied, [])


if __name__ == '__main__':
    unittest.main()


class PiPersistUseLauncherTests(unittest.TestCase):
    ORIGINAL = (
        '[Service]\n'
        'ExecStart=/opt/prusa-cam/venv/bin/python /opt/prusa-cam/persist_restore.py\n'
    )
    FIXED = (
        '[Service]\n'
        'ExecStart=/opt/prusa-cam/launcher.sh persist_restore.py\n'
    )

    def test_patches_exec_start(self):
        with tempfile.TemporaryDirectory() as root:
            unit = os.path.join(root, 'etc', 'systemd', 'system', 'pi-persist.service')
            os.makedirs(os.path.dirname(unit))
            Path(unit).write_text(self.ORIGINAL)
            # Patch out daemon-reload
            orig = migrations.subprocess.run
            migrations.subprocess.run = lambda *a, **k: None
            try:
                self.assertTrue(migrations._004_pi_persist_use_launcher(root))
            finally:
                migrations.subprocess.run = orig
            self.assertIn('launcher.sh', Path(unit).read_text())

    def test_already_patched(self):
        with tempfile.TemporaryDirectory() as root:
            unit = os.path.join(root, 'etc', 'systemd', 'system', 'pi-persist.service')
            os.makedirs(os.path.dirname(unit))
            Path(unit).write_text(self.FIXED)
            self.assertTrue(migrations._004_pi_persist_use_launcher(root))

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            unit = os.path.join(root, 'etc', 'systemd', 'system', 'pi-persist.service')
            os.makedirs(os.path.dirname(unit))
            Path(unit).write_text(self.ORIGINAL)
            orig = migrations.subprocess.run
            migrations.subprocess.run = lambda *a, **k: None
            try:
                migrations._004_pi_persist_use_launcher(root)
                self.assertTrue(migrations._004_pi_persist_use_launcher(root))
            finally:
                migrations.subprocess.run = orig
            self.assertEqual(Path(unit).read_text().count('launcher.sh'), 1)
