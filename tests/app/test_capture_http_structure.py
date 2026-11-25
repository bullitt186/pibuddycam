"""Source-structure checks for capture_http: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_capture_http.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import ast
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))
UPLOAD_PY = PI_DIR / 'upload.py'


class SnapshotUploadExpect100Tests(unittest.TestCase):
    """GAP-HTTP-01: snapshot PUT must send Expect: 100-continue.

    Inspect upload.py's AST instead of importing it, because upload.py imports
    aiohttp, which the host test environment does not provide.
    """

    def test_upload_snapshot_passes_expect100_true(self):
        tree = ast.parse(UPLOAD_PY.read_text())
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == 'upload_snapshot'
        )
        expect100_calls = [
            call
            for call in ast.walk(function)
            if isinstance(call, ast.Call)
            and any(
                kw.arg == 'expect100'
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in call.keywords
            )
        ]
        self.assertTrue(expect100_calls, 'upload_snapshot must pass expect100=True (GAP-HTTP-01)')


def _function(tree, name):
    return next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _calls_client_session(function):
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == 'ClientSession':
            return True
        if isinstance(func, ast.Name) and func.id == 'ClientSession':
            return True
    return False


class SessionReuseTests(unittest.TestCase):
    """GAP-HTTP-03: upload helpers must reuse the shared session, not build one."""

    def test_upload_helpers_do_not_construct_a_session(self):
        tree = ast.parse(UPLOAD_PY.read_text())
        for name in ('upload_snapshot', 'upload_info'):
            with self.subTest(function=name):
                self.assertFalse(
                    _calls_client_session(_function(tree, name)),
                    f'{name} must accept a session (GAP-HTTP-03)',
                )

    def test_make_session_is_the_single_factory(self):
        tree = ast.parse(UPLOAD_PY.read_text())
        self.assertTrue(_calls_client_session(_function(tree, 'make_session')))

    def test_main_does_not_construct_its_own_session(self):
        main_py = PI_DIR / 'main.py'
        tree = ast.parse(main_py.read_text())
        self.assertFalse(
            _calls_client_session(tree),
            'main.py must use upload.make_session(), not aiohttp.ClientSession directly',
        )


if __name__ == '__main__':
    unittest.main()
