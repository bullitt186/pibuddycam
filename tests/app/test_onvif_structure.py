"""Source-structure checks for onvif: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_onvif.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import ast
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))


class MainWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((PI_DIR / 'main.py').read_text())

    def test_trigger_snapshot_has_no_streaming_guard(self):
        dispatch = next(
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == 'dispatch_trigger_action'
        )
        streaming_reads = [
            node
            for node in ast.walk(dispatch)
            if isinstance(node, ast.Attribute) and node.attr == 'streaming'
        ]
        self.assertEqual(streaming_reads, [])

    def test_onvif_uses_restored_shared_state(self):
        constructor = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'create'
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == 'OnvifContext'
        ]
        self.assertEqual(len(constructor), 1)
        self.assertIsInstance(constructor[0].args[0], ast.Name)
        self.assertEqual(constructor[0].args[0].id, 'state')


if __name__ == '__main__':
    unittest.main()
