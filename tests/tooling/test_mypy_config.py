"""Checks of mypy.ini (stdlib only).

mypy reads one section per module name: a module listed in two `[mypy-...]` sections silently
loses the relaxations of one of them. The baseline sections must also name existing modules only.
"""

import configparser
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
THIRD_PARTY = {'gi', 'gi.*', 'socketio'}


def module_sections():
    config = configparser.ConfigParser()
    config.read(ROOT / 'mypy.ini', encoding='utf-8')
    for section in config.sections():
        if section.startswith('mypy-'):
            yield section, [m.strip() for m in section.removeprefix('mypy-').split(',')]


class MypyConfigTest(unittest.TestCase):
    def test_every_module_is_in_one_section_at_most(self):
        seen = {}
        for section, modules in module_sections():
            for module in modules:
                self.assertNotIn(module, seen, f'{module} in [{seen.get(module)}] and [{section}]')
                seen[module] = section

    def test_baseline_names_existing_app_modules(self):
        for section, modules in module_sections():
            for module in modules:
                if module in THIRD_PARTY:
                    continue
                self.assertTrue((ROOT / 'app' / f'{module}.py').is_file(), f'[{section}]: {module}')

    def test_strict_options_are_on_globally(self):
        config = configparser.ConfigParser()
        config.read(ROOT / 'mypy.ini', encoding='utf-8')
        for option in (
            'disallow_untyped_defs',
            'disallow_untyped_calls',
            'check_untyped_defs',
            'disallow_any_generics',
            'warn_return_any',
            'strict_equality',
        ):
            self.assertTrue(config.getboolean('mypy', option), option)


if __name__ == '__main__':
    unittest.main()
