"""Source-structure checks for settings_coordinator: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_settings_coordinator.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import ast
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))
MAIN_PY = PI_DIR / 'main.py'


class MainWiringStaticTests(unittest.TestCase):
    """main.py is not importable on the host; inspect it as text/AST."""

    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text()
        cls.tree = ast.parse(cls.source)

    def _functions(self, name):
        return [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        ]

    def test_references_settings_coordinator(self):
        self.assertIn('SettingsCoordinator', self.source)
        self.assertIn('SettingsCoordinator(', self.source)

    def test_no_direct_save_persisted_state_calls(self):
        calls = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == '_save_persisted_state'
        ]
        self.assertEqual(calls, [])

    def test_command_paths_route_through_coordinator(self):
        for name in ('handle_event', 'dispatch_trigger_action'):
            functions = self._functions(name)
            self.assertTrue(functions, name)
            for function in functions:
                calls = [n for n in ast.walk(function) if isinstance(n, ast.Call)]
                self.assertTrue(
                    any(
                        isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == 'coordinator'
                        for call in calls
                    ),
                    f'{name} does not route through the coordinator',
                )

    def test_main_does_not_write_settings_store(self):
        calls = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'save'
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == 'settings_store'
        ]
        self.assertEqual(calls, [])


class SettingsStoreWriterTests(unittest.TestCase):
    """AC-4: only the coordinator (plus importer/restore) writes state.json."""

    def test_only_coordinator_writes_settings_store(self):
        allowed = {
            'settings_store.py',
            'persist_restore.py',
            'settings_coordinator.py',
        }
        callers = set()
        for path in sorted(PI_DIR.glob('*.py')):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == 'save'
                    and isinstance(func.value, ast.Name)
                    and func.value.id == 'settings_store'
                ):
                    callers.add(path.name)

        self.assertTrue(callers <= allowed, f'unexpected writers: {sorted(callers - allowed)}')
        self.assertIn('settings_coordinator.py', callers)


if __name__ == '__main__':
    unittest.main()
