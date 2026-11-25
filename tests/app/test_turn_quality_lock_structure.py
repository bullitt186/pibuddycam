"""Source-structure checks for turn_quality_lock: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_turn_quality_lock.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import ast
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
MAIN_PY = PI_DIR / 'main.py'
WEBRTC_PY = PI_DIR / 'webrtc.py'
QUALITY_CONTROL_PY = PI_DIR / 'quality_control.py'
sys.path.insert(0, str(PI_DIR))


class _AstHelpers:
    def _function(self, tree, name):
        return next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        )

    def _call_named(self, node, name):
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Name) and call.func.id == name:
                return call
            if isinstance(call.func, ast.Attribute) and call.func.attr == name:
                return call
        return None

    def _attr_assignment_values(self, node, attr_name):
        values = []
        for assign in ast.walk(node):
            if not isinstance(assign, ast.Assign):
                continue
            for target in assign.targets:
                if isinstance(target, ast.Attribute) and target.attr == attr_name:
                    values.append(assign.value)
        return values


class MainWiringTests(unittest.TestCase, _AstHelpers):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(MAIN_PY.read_text())

    def test_relay_candidate_sets_turn_online(self):
        fn = self._function(self.tree, 'handle_event')
        self.assertIsNotNone(self._call_named(fn, 'parse_candidate_type'))
        values = self._attr_assignment_values(fn, 'turn_online')
        self.assertIn(True, [v.value for v in values if isinstance(v, ast.Constant)])

    def test_stream_end_clears_turn_online(self):
        fn = self._function(self.tree, 'on_stream_ended')
        values = self._attr_assignment_values(fn, 'turn_online')
        self.assertIn(False, [v.value for v in values if isinstance(v, ast.Constant)])

    def test_teardown_callback_clears_turn_online(self):
        fn = self._function(self.tree, 'on_teardown')
        values = self._attr_assignment_values(fn, 'turn_online')
        self.assertIn(False, [v.value for v in values if isinstance(v, ast.Constant)])

    def test_quality_path_consults_state_and_helper_inputs(self):
        fn = self._function(self.tree, 'handle_quality')
        call = self._call_named(fn, 'handle_quality')
        self.assertIsNotNone(call)
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        self.assertIn('current_enum', kwargs)
        self.assertIn('turn_online', kwargs)
        self.assertIsInstance(kwargs['turn_online'], ast.Attribute)
        self.assertEqual(kwargs['turn_online'].attr, 'turn_online')

    def test_prusa_webrtc_receives_on_teardown(self):
        constructor = self._call_named(self.tree, 'PrusaWebRTC')
        self.assertIsNotNone(constructor)
        kwargs = {kw.arg: kw.value for kw in constructor.keywords}
        self.assertIn('on_teardown', kwargs)
        self.assertIsInstance(kwargs['on_teardown'], ast.Name)
        self.assertEqual(kwargs['on_teardown'].id, 'on_teardown')


class QualityControlWiringTests(unittest.TestCase, _AstHelpers):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(QUALITY_CONTROL_PY.read_text())

    def test_handle_quality_consults_pure_helper(self):
        fn = self._function(self.tree, 'handle_quality')
        self.assertIsNotNone(self._call_named(fn, 'quality_change_allowed'))


class WebRtcModuleWiringTests(unittest.TestCase, _AstHelpers):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(WEBRTC_PY.read_text())

    def test_teardown_notifies_owner(self):
        fn = self._function(self.tree, '_teardown')
        self.assertIsNotNone(self._call_named(fn, '_notify_teardown'))

    def test_notify_teardown_invokes_callback(self):
        fn = self._function(self.tree, '_notify_teardown')
        self.assertTrue(
            any(isinstance(n, ast.Attribute) and n.attr == '_on_teardown' for n in ast.walk(fn))
        )


if __name__ == '__main__':
    unittest.main()
