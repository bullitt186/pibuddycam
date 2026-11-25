"""Source-structure checks for webrtc_connection_info: they read files of app/ instead of running the code.

Kept apart from the behaviour tests in test_webrtc_connection_info.py so mutation testing can skip them
(scripts/mutate.sh ignores tests/app/*_structure.py, #33).
"""

import ast
import sys
import types
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
MAIN_PY = PI_DIR / 'main.py'
SIGNALING_PY = PI_DIR / 'signaling.py'
WEBRTC_PY = PI_DIR / 'webrtc.py'
sys.path.insert(0, str(PI_DIR))
_socketio_stub = types.ModuleType('socketio')
_socketio_stub.AsyncClient = object
sys.modules.setdefault('socketio', _socketio_stub)


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


class SignalingWiringTests(unittest.TestCase, _AstHelpers):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(SIGNALING_PY.read_text())

    def test_sender_method_emits_confirmed_event(self):
        fn = self._function(self.tree, 'send_webrtc_connection_info')
        self.assertIsInstance(fn, ast.AsyncFunctionDef)
        event_names = {
            n.value
            for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        self.assertIn('webrtc_connection_info', event_names)
        self.assertIsNotNone(self._call_named(fn, 'encode_message'))

    def test_sender_field_map_is_one_two_three(self):
        fn = self._function(self.tree, 'send_webrtc_connection_info')
        dicts = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Dict)
            and any(isinstance(k, ast.Constant) and k.value == 1 for k in n.keys)
        ]
        self.assertEqual(len(dicts), 1)
        keys = {k.value for k in dicts[0].keys}
        self.assertEqual(keys, {1, 2, 3})


class MainWiringTests(unittest.TestCase, _AstHelpers):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(MAIN_PY.read_text())

    def test_prusa_webrtc_receives_on_connection_info(self):
        constructor = self._call_named(self.tree, 'PrusaWebRTC')
        self.assertIsNotNone(constructor)
        kwargs = {kw.arg: kw.value for kw in constructor.keywords}
        self.assertIn('on_connection_info', kwargs)
        self.assertIsInstance(kwargs['on_connection_info'], ast.Name)
        self.assertEqual(kwargs['on_connection_info'].id, 'on_connection_info')

    def test_callback_delegates_to_signaling_sender(self):
        fn = self._function(self.tree, 'on_connection_info')
        self.assertIsInstance(fn, ast.AsyncFunctionDef)
        self.assertIsNotNone(self._call_named(fn, 'send_webrtc_connection_info'))


class WebRtcModuleWiringTests(unittest.TestCase, _AstHelpers):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(WEBRTC_PY.read_text())

    def test_connected_state_emits_connection_info(self):
        fn = self._function(self.tree, '_on_ice_state_change')
        self.assertIsNotNone(self._call_named(fn, '_emit_connection_info'))
        self.assertTrue(
            any(isinstance(n, ast.Attribute) and n.attr == 'ICE_CONNECTED' for n in ast.walk(fn))
        )

    def test_emit_uses_recovered_codes_and_skips_without_stats(self):
        emit = self._function(self.tree, '_emit_connection_info')
        # get-stats must be requested with an async change callback; calling
        # promise.wait() on the GLib thread would deadlock.
        self.assertIsNotNone(self._call_named(emit, 'new_with_change_func'))
        self.assertTrue(
            any(isinstance(n, ast.Constant) and n.value == 'get-stats' for n in ast.walk(emit))
        )
        self.assertFalse(
            any(isinstance(n, ast.Attribute) and n.attr == 'wait' for n in ast.walk(emit))
        )
        ready = self._function(self.tree, '_on_stats_ready')
        self.assertIsNotNone(self._call_named(ready, 'candidate_type_code'))
        # An unextractable stats shape must skip, not misreport a forced 6/6
        # (the numeric encoding was also rejected live as "Read past limit").
        self.assertFalse(
            any(
                isinstance(n, ast.Attribute) and n.attr == 'CANDIDATE_TYPE_NO_PAIR'
                for n in ast.walk(ready)
            )
        )


if __name__ == '__main__':
    unittest.main()
