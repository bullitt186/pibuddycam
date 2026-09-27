"""WP-R1 B3: the fixed-verb root helper and its narrow sudoers rule.

Runs the helper with a stubbed PATH / app root so no privileged command, no
NetworkManager and no service is touched. The sudoers rule is validated with
``visudo -cf`` when available.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PIBUDDYCAM_PRIV = REPO / 'image' / 'assets' / 'pibuddycam-priv'
SUDOERS = REPO / 'image' / 'assets' / 'sudoers' / 'pibuddycam'


def run_helper(args, stdin='', env=None, path_prepend=None):
    environ = dict(os.environ)
    if path_prepend:
        environ['PATH'] = f'{path_prepend}:{environ.get("PATH", "")}'
    environ.update(env or {})
    return subprocess.run(
        ['bash', str(PIBUDDYCAM_PRIV), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=environ,
    )


class PrusaPrivAssetTests(unittest.TestCase):
    def test_helper_exists_and_is_executable(self):
        self.assertTrue(PIBUDDYCAM_PRIV.is_file())
        self.assertTrue(os.access(PIBUDDYCAM_PRIV, os.X_OK))

    def test_helper_passes_bash_n(self):
        result = subprocess.run(
            ['bash', '-n', str(PIBUDDYCAM_PRIV)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_helper_declares_exactly_the_allowlisted_verbs(self):
        text = PIBUDDYCAM_PRIV.read_text(encoding='utf-8')
        for verb in (
            'start-camera',
            'stop-provisioning',
            'hotspot-start',
            'hotspot-stop',
            'wifi-station-apply',
            'install-update',
            'check-update',
            'reboot',
            'rtsp-start',
            'rtsp-stop',
            'quality-restart',
        ):
            self.assertIn(f'{verb})', text)
        # Nothing else is dispatched.
        self.assertIn('*)\n      exit 2', text)

    def test_unknown_verb_exits_two(self):
        result = run_helper(['definitely-not-a-verb'])
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_wifi_station_apply_reads_psk_from_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / 'app'
            python = app_root / 'venv' / 'bin' / 'python'
            python.parent.mkdir(parents=True)
            args_out = Path(tmp) / 'args'
            stdin_out = Path(tmp) / 'stdin'
            python.write_text(
                '#!/bin/sh\n'
                f'printf "%s\\n" "$@" > "{args_out}"\n'
                f'cat > "{stdin_out}"\n',
                encoding='utf-8',
            )
            python.chmod(0o755)

            result = run_helper(
                ['wifi-station-apply', 'HomeNet'],
                stdin='sup3rsecret',
                env={'PIBUDDYCAM_APP_ROOT': str(app_root)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = args_out.read_text(encoding='utf-8')
            self.assertIn('wifi_station.py', argv)
            self.assertIn('apply', argv)
            self.assertIn('HomeNet', argv)
            self.assertNotIn('sup3rsecret', argv)
            self.assertEqual(stdin_out.read_text(encoding='utf-8'), 'sup3rsecret')

    def test_wifi_station_apply_requires_ssid(self):
        result = run_helper(['wifi-station-apply'])
        self.assertEqual(result.returncode, 2)

    def test_start_camera_uses_absolute_systemctl_and_target(self):
        # Text-level: the helper must not rely on the caller's PATH for a root
        # command, so it pins the absolute systemctl path. (Executing the verb
        # here would run the host's real systemctl; the stub-path approach no
        # longer applies once the path is absolute.)
        text = PIBUDDYCAM_PRIV.read_text(encoding='utf-8')
        self.assertIn('SYSTEMCTL=/usr/bin/systemctl', text)
        self.assertIn('exec "$SYSTEMCTL" --no-block start pibuddycam.target', text)
        self.assertIn('exec "$SYSTEMCTL" stop pibuddycam-provisioning.service', text)
        self.assertIn('exec "$SYSTEMCTL" start pibuddycam-updater-install.service', text)
        self.assertIn('exec "$SYSTEMCTL" start pibuddycam-updater.service', text)
        self.assertIn('exec "$SYSTEMCTL" reboot', text)
        self.assertIn(
            '"$SYSTEMCTL" restart rpicam-source.service pibuddycam-ha-rtsp.service',
            text,
        )
        self.assertIn(
            'exec "$SYSTEMCTL" try-restart pibuddycam-rtsp.service', text
        )
        self.assertNotIn('\n      exec systemctl', text)


class UpdaterUnitBoundaryTests(unittest.TestCase):
    """WP-UI7/AC-15: the helper invokes the authoritative existing units.

    The repository's ``pibuddycam-updater.service`` (``check``) and
    ``pibuddycam-updater-install.service`` (``install``) are the fixed signed-update
    path; the helper must never invent a different unit or command.
    """

    def setUp(self):
        self.helper = PIBUDDYCAM_PRIV.read_text(encoding='utf-8')
        self.systemd = REPO / 'app' / 'systemd'

    def test_check_update_starts_the_report_only_check_unit(self):
        block = self.helper.split('check-update)', 1)[1].split(';;', 1)[0]
        self.assertIn('start pibuddycam-updater.service', block)
        self.assertNotIn('pibuddycam-updater-install', block)
        unit = (self.systemd / 'pibuddycam-updater.service').read_text(encoding='utf-8')
        exec_lines = [
            line for line in unit.splitlines() if line.startswith('ExecStart=')
        ]
        self.assertEqual(len(exec_lines), 1)
        self.assertIn('updater_install.py check', exec_lines[0])

    def test_install_update_starts_the_signed_install_unit(self):
        block = self.helper.split('install-update)', 1)[1].split(';;', 1)[0]
        self.assertIn('start pibuddycam-updater-install.service', block)
        unit = (self.systemd / 'pibuddycam-updater-install.service').read_text(
            encoding='utf-8')
        exec_lines = [
            line for line in unit.splitlines() if line.startswith('ExecStart=')
        ]
        self.assertEqual(len(exec_lines), 1)
        self.assertIn('updater_install.py install', exec_lines[0])

    def test_reboot_is_the_fixed_systemctl_reboot(self):
        block = self.helper.split('reboot)', 1)[1].split(';;', 1)[0]
        self.assertIn('exec "$SYSTEMCTL" reboot', block)


class SudoersAssetTests(unittest.TestCase):
    def test_rule_grants_only_the_helper(self):
        text = SUDOERS.read_text(encoding='utf-8')
        self.assertIn(
            'pibuddycam ALL=(root) NOPASSWD: /usr/libexec/pibuddycam/pibuddycam-priv',
            text,
        )
        grant = text.split('NOPASSWD:', 1)[1]
        self.assertNotIn('ALL', grant)
        self.assertNotIn('*', grant)

    def test_rule_pins_env_reset_and_secure_path(self):
        # The privilege boundary must not depend on the base image's global sudo
        # defaults: env_reset blocks PIBUDDYCAM_APP_ROOT/PATH injection.
        text = SUDOERS.read_text(encoding='utf-8')
        self.assertIn('Defaults:pibuddycam env_reset', text)
        self.assertIn('Defaults:pibuddycam secure_path=', text)

    def test_visudo_accepts_the_file(self):
        visudo = shutil.which('visudo')
        if visudo is None:
            self.skipTest('visudo not available')
        result = subprocess.run(
            [visudo, '-cf', str(SUDOERS)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_install_sets_root_owner_and_0440(self):
        installer = (REPO / 'image' / 'assets' / 'install-factory-app.sh').read_text(
            encoding='utf-8'
        )
        self.assertIn('-o root -g root -m 0440', installer)
        self.assertIn('/etc/sudoers.d/pibuddycam', installer)
        self.assertIn('visudo -cf', installer)


if __name__ == '__main__':
    unittest.main()
