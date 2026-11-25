"""Checks that tests reading app/ files live in tests/app/*_structure.py (stdlib only, #33).

scripts/mutate.sh skips the structure files: under mutmut they would read the mutated, many
times larger sources, and they cannot kill a mutant because they do not run the code. Every
other test file in tests/app/ must therefore run app code instead of reading app files, or its
behaviour tests would be lost for mutation testing when it gets ignored.
"""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_PATH = re.compile(r"'[a-z_0-9]+\.py'|'app'|'systemd'")
MODULE_FILE = re.compile(r'\b[a-z_]+\.__file__')
SOURCE_CALLS = {('ast', 'parse'), ('inspect', 'getsource'), ('inspect', 'getsourcelines')}


def app_path_constants(tree, source):
    """Module-level names bound to a path below app/ (built from __file__ or another such name)."""
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            text = ast.get_source_segment(source, node.value) or ''
            used = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            if isinstance(target, ast.Name) and (
                (APP_PATH.search(text) and ('__file__' in text or used & names))
                or (used & names and isinstance(node.value, ast.BinOp))
            ):
                names.add(target.id)
    return names


def reads_of_app_files(path):
    """Line numbers of calls that read or parse app source in one test file."""
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    paths = app_path_constants(tree, source)
    found = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        receiver = None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if (func.value.id, func.attr) in SOURCE_CALLS:
                found.append(call.lineno)
                continue
        if isinstance(func, ast.Attribute) and func.attr in ('read_text', 'read_bytes'):
            receiver = func.value
        elif isinstance(func, ast.Name) and func.id in ('open', 'parse_unit') and call.args:
            receiver = call.args[0]
        if receiver is None:
            continue
        text = ast.get_source_segment(source, receiver) or ''
        used = {n.id for n in ast.walk(receiver) if isinstance(n, ast.Name)}
        if MODULE_FILE.search(text) or ('__file__' in text and APP_PATH.search(text)) or used & paths:
            found.append(call.lineno)
    return found


class StructureTestsTest(unittest.TestCase):
    def test_behaviour_test_files_do_not_read_app_files(self):
        for path in sorted((ROOT / 'tests' / 'app').glob('test_*.py')):
            if path.stem.endswith('_structure'):
                continue
            with self.subTest(file=path.name):
                self.assertEqual(
                    reads_of_app_files(path),
                    [],
                    'reads or parses app/ files: move these tests to '
                    f'{path.stem}_structure.py (see docs/development.md)',
                )

    def test_the_check_finds_source_reads(self):
        # The patterns the structure files use; if one stops matching, the check above is blind.
        for name in ('test_main_mqtt_structure.py', 'test_admin_web_structure.py',
                     'test_camera_probe_structure.py', 'test_persist_restore_structure.py',
                     'test_main_token_guard_structure.py', 'test_image_guard_structure.py'):
            with self.subTest(file=name):
                self.assertNotEqual(reads_of_app_files(ROOT / 'tests' / 'app' / name), [])

    def test_mutation_run_ignores_exactly_the_structure_files(self):
        script = (ROOT / 'scripts' / 'mutate.sh').read_text(encoding='utf-8')
        self.assertIn("tests/app/*_structure.py", script)
        self.assertNotIn('grep -lE', script)


if __name__ == '__main__':
    unittest.main()
