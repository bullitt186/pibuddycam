"""Source-structure checks for persist_restore: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_persist_restore.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import ast
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))
import persist_restore  # noqa: E402


class ImportSafetyTests(unittest.TestCase):
    """Importing must not touch the filesystem (the module runs only via main)."""

    def test_no_module_level_io_calls(self):
        tree = ast.parse(Path(persist_restore.__file__).read_text())
        forbidden = {
            'makedirs',
            'mkdir',
            'remove',
            'unlink',
            'chown',
            'write_current',
            'write_mode',
            'save',
            'run',
        }
        offenders = []
        for node in tree.body:
            # Function/class bodies only run when called; skip them.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else (func.id if isinstance(func, ast.Name) else '')
                )
                if name in forbidden:
                    offenders.append(name)
        self.assertEqual(offenders, [])


if __name__ == '__main__':
    unittest.main()
