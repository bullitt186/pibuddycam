"""Source-structure checks for device_control: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_device_control.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import ast
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))
MAIN_PY = PI_DIR / 'main.py'


class MainWiringTests(unittest.TestCase):
    """AST checks that main.py wires the reboot action to the narrow command.

    main.py imports aiohttp/socketio, so parse its AST instead of importing it.
    """

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(MAIN_PY.read_text())

    def _function(self, name):
        return next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        )

    def test_reboot_device_runs_only_the_narrow_systemd_command(self):
        runs = [
            node
            for node in ast.walk(self._function('reboot_device'))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'run'
        ]
        self.assertEqual(len(runs), 1)
        command = runs[0].args[0]
        self.assertIsInstance(command, ast.List)
        self.assertEqual([elt.value for elt in command.elts], ['sudo', 'systemctl', 'reboot'])

    def test_dispatch_calls_request_reboot_with_the_injected_reboot_fn(self):
        dispatcher = self._function('dispatch_trigger_action')
        calls = [
            node
            for node in ast.walk(dispatcher)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'request_reboot'
        ]
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0].func.value, ast.Name)
        self.assertEqual(calls[0].func.value.id, 'device_control')
        self.assertEqual(len(calls[0].args), 2)
        self.assertIsInstance(calls[0].args[1], ast.Name)
        self.assertEqual(calls[0].args[1].id, 'reboot_device')

    def test_reboot_device_is_injected_and_never_called_directly(self):
        # It is only ever passed as an injected callable: to the trigger
        # dispatcher and as the MQTT restart action. It must never be the
        # callee of a direct call.
        references = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Name) and node.id == 'reboot_device'
        ]
        self.assertEqual(len(references), 2)
        direct_calls = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == 'reboot_device'
        ]
        self.assertEqual(direct_calls, [])
        # The second reference is the MQTT service's restart callable.
        keyword_refs = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.keyword)
            and node.arg == 'restart'
            and isinstance(node.value, ast.Name)
            and node.value.id == 'reboot_device'
        ]
        self.assertEqual(len(keyword_refs), 1)


if __name__ == '__main__':
    unittest.main()
