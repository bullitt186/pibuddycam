"""Source-structure checks for timelapse: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_timelapse.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import ast
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
MAIN_PY = PI_DIR / 'main.py'
sys.path.insert(0, str(PI_DIR))


class MainTimelapseWiringTests(unittest.TestCase):
    """AST checks for main.py's file-list sender.

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

    def test_file_list_sender_guards_empty_and_calls_send_file_list(self):
        fn = self._function('_send_timelapse_file_list')
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        self.assertTrue(
            any(
                isinstance(n.func, ast.Attribute) and n.func.attr == 'file_list_entries'
                for n in calls
            )
        )
        self.assertTrue(
            any(
                isinstance(n.func, ast.Attribute) and n.func.attr == 'send_file_list' for n in calls
            )
        )
        guards = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.UnaryOp)
            and isinstance(n.test.op, ast.Not)
            and isinstance(n.test.operand, ast.Name)
            and n.test.operand.id == 'listing'
            and any(isinstance(b, ast.Return) for b in n.body)
        ]
        self.assertEqual(len(guards), 1)

    def test_direct_event_request_id_prefers_trigger_tag_11(self):
        fn = self._function('_request_id_from_event')
        # Prefer the recovered trigger request-id field (tag 11) over the
        # best-effort "first non-empty string" fallback.
        self.assertTrue(
            any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'get'
                and n.args
                and isinstance(n.args[0], ast.Constant)
                and n.args[0].value == 11
                for n in ast.walk(fn)
            )
        )

    def test_event_handler_routes_mutations_through_coordinator(self):
        # GAP-PERSIST-01 / WP-1 AC-4: every successful mutation in handle_event
        # persists through the shared settings coordinator, which owns the
        # durable-state write. _save_persisted_state remains the injected
        # persist callback.
        self._function('_save_persisted_state')
        fn = self._function('handle_event')
        calls = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == 'coordinator'
        ]
        self.assertGreaterEqual(len(calls), 6)

    def test_startup_loads_and_restores_persisted_settings(self):
        fn = self._function('main')
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        self.assertTrue(
            any(
                isinstance(n.func, ast.Attribute)
                and n.func.attr == 'load'
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == 'settings_store'
                for n in calls
            )
        )
        # WP-1 AC-4: startup applies persisted settings through the shared
        # settings coordinator (which owns persistence and publication).
        self.assertTrue(
            any(
                isinstance(n.func, ast.Attribute)
                and n.func.attr == 'restore'
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == 'coordinator'
                for n in calls
            )
        )

    def test_config_handler_maps_tag3_5_to_snapshot_interval(self):
        # GAP-CONFIG-01: configuration tag3.5 = set_snapshot_upload_interval
        # (FUN_000a7940, 10..600). Connect's update-interval slider sends it.
        fn = self._function('handle_event')
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        self.assertTrue(
            any(
                isinstance(n.func, ast.Attribute) and n.func.attr == 'set_snapshot_interval'
                for n in calls
            )
        )
        self.assertTrue(
            any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'get'
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == 't3'
                and n.args
                and isinstance(n.args[0], ast.Constant)
                and n.args[0].value == 5
                for n in calls
            )
        )


if __name__ == '__main__':
    unittest.main()
