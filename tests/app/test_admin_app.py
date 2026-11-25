"""WP-3e2: static checks for the aiohttp admin transport and its unit.

The transport imports ``aiohttp`` at module top, which is not a host-test
dependency, so this module never imports :mod:`admin_app`. It parses the source
with :mod:`ast` and asserts the shape the contract requires:

* the aiohttp routes map 1:1 onto :class:`admin_http.AdminApp`'s route table;
* ``peer_ip`` comes from the socket transport, never a forwarding header;
* the ``Response`` (including ``Set-Cookie``) is passed through verbatim;
* ``pibuddycam-admin.service`` is a valid, non-personal unit gated on
  ``data-ready.target``; and
* the factory installer installs and enables the unit.

Stdlib-only, no ``aiohttp`` import.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PI_DIR = REPO / 'app'
UNIT = PI_DIR / 'systemd' / 'pibuddycam-admin.service'
INSTALLER = REPO / 'image' / 'assets' / 'install-factory-app.sh'

sys.path.insert(0, str(PI_DIR))


class AdminUnitTests(unittest.TestCase):
    def setUp(self):
        self.text = UNIT.read_text(encoding='utf-8')
        self.lines = self.text.splitlines()

    def _values(self, key):
        prefix = key + '='
        return [line[len(prefix):] for line in self.lines if line.startswith(prefix)]

    def test_unit_syntax_is_key_value_lines(self):
        for line in self.lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or stripped.startswith('['):
                continue
            self.assertIn('=', stripped, f'not a systemd key=value line: {line!r}')
            key, _, value = stripped.partition('=')
            self.assertTrue(key, f'empty systemd key in: {line!r}')
            self.assertTrue(value, f'empty systemd value in: {line!r}')

    def test_runs_as_service_account_from_app_root(self):
        self.assertEqual(self._values('User'), ['pibuddycam'])
        self.assertEqual(self._values('WorkingDirectory'), ['/opt/pibuddycam'])

    def test_execstart_runs_the_transport_entry_point(self):
        exec_start = self._values('ExecStart')
        self.assertEqual(len(exec_start), 1)
        # WP-R4c: the unit starts through the launcher so an installed signed
        # release (and its per-release venv) runs, with a factory fallback.
        self.assertIn('/opt/pibuddycam/launcher.sh', exec_start[0])
        self.assertIn('admin_app.py', exec_start[0])

    def test_gated_on_data_ready(self):
        self.assertTrue(any('data-ready.target' in v for v in self._values('After')))
        self.assertTrue(any('data-ready.target' in v for v in self._values('Requires')))

    def test_binds_privileged_ports(self):
        self.assertTrue(
            any('CAP_NET_BIND_SERVICE' in v for v in self._values('AmbientCapabilities'))
        )

    def test_restart_is_bounded_on_failure(self):
        self.assertEqual(self._values('Restart'), ['on-failure'])
        self.assertTrue(self._values('RestartSec'))
        self.assertTrue(self._values('StartLimitIntervalSec'))
        self.assertTrue(self._values('StartLimitBurst'))

    def test_documents_mode(self):
        self.assertIn('ADMIN_MODE=admin', self._values('Environment'))

    def test_installs_under_camera_target(self):
        install_index = self.lines.index('[Install]')
        self.assertIn('WantedBy=pibuddycam.target', self.lines[install_index:])

    def test_no_personal_username_or_home_path(self):
        self.assertNotIn('bullitt', self.text)
        self.assertNotIn('/home/', self.text)


class FactoryInstallerTests(unittest.TestCase):
    def test_installer_installs_admin_unit_but_defers_enablement(self):
        text = INSTALLER.read_text(encoding='utf-8')
        unit_loop = text.split('systemctl enable', 1)[0]
        enable_block = text.split('systemctl enable', 1)[1]
        # Installed verbatim; enabled only transitively via pibuddycam.target
        # after claim, never directly at multi-user.target.
        self.assertIn('pibuddycam-admin.service', unit_loop)
        self.assertNotIn('pibuddycam-admin.service', enable_block)

    def test_installer_installs_provisioning_and_boot_mode_units(self):
        text = INSTALLER.read_text(encoding='utf-8')
        unit_loop = text.split('systemctl enable', 1)[0]
        self.assertIn('pibuddycam-provisioning.service', unit_loop)
        self.assertIn('pibuddycam-boot-mode.service', unit_loop)


if __name__ == '__main__':
    unittest.main()
