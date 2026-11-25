"""GAP-WEBRTC-03: WebRTC session lifecycle and state cleanup wiring.

The pure policy (``webrtc_lifecycle``) is imported directly. ``main.py`` and
``webrtc.py`` depend on aiohttp/socketio and PyGObject/GStreamer respectively, so
their wiring is checked with the AST pattern from
``test_timelapse.MainTimelapseWiringTests``.
"""
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import webrtc_lifecycle  # noqa: E402


class EndReasonTests(unittest.TestCase):
    def test_all_states_map_correctly(self):
        self.assertIsNone(webrtc_lifecycle.end_reason(0))   # NEW
        self.assertIsNone(webrtc_lifecycle.end_reason(1))   # CHECKING
        self.assertIsNone(webrtc_lifecycle.end_reason(2))   # CONNECTED
        self.assertIsNone(webrtc_lifecycle.end_reason(3))   # COMPLETED
        self.assertEqual(webrtc_lifecycle.end_reason(4), 'ice-failed')
        self.assertEqual(webrtc_lifecycle.end_reason(5), 'ice-disconnected')
        self.assertEqual(webrtc_lifecycle.end_reason(6), 'ice-closed')

    def test_unknown_state_is_not_terminal(self):
        self.assertIsNone(webrtc_lifecycle.end_reason(99))
        self.assertIsNone(webrtc_lifecycle.end_reason(None))

    def test_connected_and_ended_membership(self):
        self.assertEqual(webrtc_lifecycle.ICE_CONNECTED, frozenset({2, 3}))
        self.assertEqual(webrtc_lifecycle.ICE_ENDED, frozenset({4, 6}))
        self.assertEqual(webrtc_lifecycle.ICE_DISCONNECTED, 5)
        self.assertTrue(webrtc_lifecycle.ICE_CONNECTED.isdisjoint(webrtc_lifecycle.ICE_ENDED))




if __name__ == '__main__':
    unittest.main()
