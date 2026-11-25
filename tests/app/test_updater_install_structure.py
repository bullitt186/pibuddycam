"""Source-structure checks for updater_install: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_updater_install.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import ast
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))


def parse_unit(path):
    sections = {}
    current = None
    for raw in Path(path).read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or line.startswith(';'):
            continue
        if line.startswith('[') and line.endswith(']'):
            current = line[1:-1]
            sections.setdefault(current, {})
            continue
        if '=' in line and current is not None:
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip()
            existing = sections[current].get(key)
            sections[current][key] = value if existing is None else f'{existing} {value}'
    return sections


class UpdaterUnitTests(unittest.TestCase):
    def test_service_is_root_oneshot_gated_on_data_ready(self):
        unit = parse_unit(PI_DIR / 'systemd' / 'pibuddycam-updater.service')
        section = unit['Unit']
        self.assertIn('data-ready.target', section['Requires'].split())
        self.assertIn('data-ready.target', section['After'].split())
        service = unit['Service']
        self.assertEqual(service['Type'], 'oneshot')
        self.assertEqual(service['User'], 'root')
        self.assertIn('updater_install.py', service['ExecStart'])
        self.assertIn(' check', service['ExecStart'])
        # A documented fixed PATH for the minisign/zstd binaries.
        self.assertIn('PATH=', service['Environment'])
        # The oneshot service is triggered by the timer, not enabled directly.
        self.assertNotIn('Install', unit)

    def test_service_recovers_before_check(self):
        unit = parse_unit(PI_DIR / 'systemd' / 'pibuddycam-updater.service')
        service = unit['Service']
        self.assertIn('updater_install.py recover', service['ExecStartPre'])
        self.assertIn(' check', service['ExecStart'])

    def test_service_config_is_root_owned_not_service_writable(self):
        text = (PI_DIR / 'systemd' / 'pibuddycam-updater.service').read_text(encoding='utf-8')
        service = parse_unit(PI_DIR / 'systemd' / 'pibuddycam-updater.service')['Service']
        # AC-32: the manifest URL comes from the root-owned file, never from
        # /etc/pibuddycam (which the unprivileged service account can write).
        self.assertEqual(service.get('EnvironmentFile'), '-/etc/pibuddycam-updater.conf')
        self.assertNotIn('/etc/pibuddycam/', text)
        # The trust anchor cannot be overridden from the environment.
        self.assertNotIn('PIBUDDYCAM_UPDATE_PUBLIC_KEY', text)
        # The module itself must never read a public-key env override.
        source = (PI_DIR / 'updater_install.py').read_text(encoding='utf-8')
        self.assertNotIn('PIBUDDYCAM_UPDATE_PUBLIC_KEY', source)
        self.assertNotIn('PUBLIC_KEY_ENV', source)

    def test_timer_triggers_service_and_is_enabled(self):
        unit = parse_unit(PI_DIR / 'systemd' / 'pibuddycam-updater.timer')
        self.assertEqual(unit['Timer']['Unit'], 'pibuddycam-updater.service')
        self.assertIn('24h', unit['Timer']['OnUnitActiveSec'])
        self.assertIn('multi-user.target', unit['Install']['WantedBy'].split())


class ImportSafetyTests(unittest.TestCase):
    def test_source_is_stdlib_plus_local(self):
        source = (PI_DIR / 'updater_install.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split('.')[0])
        allowed_stdlib = {
            'argparse',
            'dataclasses',
            'json',
            'logging',
            'os',
            'random',
            're',
            'shutil',
            'socket',
            'subprocess',
            'sys',
            'tempfile',
            'time',
            'urllib',
        }
        self.assertTrue(
            imported <= allowed_stdlib | {'app_version', 'updater'},
            f'unexpected imports: {imported}',
        )


if __name__ == '__main__':
    unittest.main()
