"""GAP-WEBRTC-05 TURN/scoped-quality lock.

Pure policy helper (``quality.quality_change_allowed``), the enforcement choke
point (``quality_control.handle_quality``), and the ``main.py``/``webrtc.py``
wiring. ``main.py`` and ``webrtc.py`` depend on aiohttp/PyGObject, so their
wiring is checked with the AST pattern used by the other WebRTC tests.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import quality  # noqa: E402
import quality_control  # noqa: E402
from state import CameraState  # noqa: E402


class QualityChangeAllowedTests(unittest.TestCase):
    """turn_online False -> always allowed; True -> requested <= current."""

    def test_turn_offline_allows_every_change(self):
        for current, requested in ((1, 3), (3, 1), (2, 2), (1, 1), (3, 3)):
            with self.subTest(current=current, requested=requested):
                self.assertTrue(
                    quality.quality_change_allowed(current, requested, False)
                )

    def test_turn_online_allows_equal_and_lower(self):
        self.assertTrue(quality.quality_change_allowed(3, 3, True))
        self.assertTrue(quality.quality_change_allowed(3, 1, True))
        self.assertTrue(quality.quality_change_allowed(2, 2, True))

    def test_turn_online_rejects_raise(self):
        self.assertFalse(quality.quality_change_allowed(1, 3, True))
        self.assertFalse(quality.quality_change_allowed(1, 2, True))
        self.assertFalse(quality.quality_change_allowed(2, 3, True))

    def test_unknown_current_tier_does_not_block(self):
        self.assertTrue(quality.quality_change_allowed(None, 3, True))


class TurnLockHandleQualityTests(unittest.TestCase):
    """The lock rejects a raise before any live or persisted effect."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_envs = (quality.QUALITY_ENV, quality.QUALITY_LIVE_ENV)
        quality.QUALITY_ENV = os.path.join(self._tmp.name, 'quality.env')
        quality.QUALITY_LIVE_ENV = os.path.join(self._tmp.name, 'quality.live.env')

    def tearDown(self):
        quality.QUALITY_ENV, quality.QUALITY_LIVE_ENV = self._old_envs
        self._tmp.cleanup()

    def test_relay_online_rejects_raise_before_live_apply(self):
        state = CameraState(quality=1)  # SD
        live_calls = []
        persist_calls = []

        ok = quality_control.handle_quality(
            7, True, lambda raw: live_calls.append(raw) or True,
            persist_calls.append, current_enum=state.quality, turn_online=True,
        )

        self.assertFalse(ok)
        self.assertEqual(live_calls, [])
        self.assertEqual(persist_calls, [])
        self.assertEqual(state.quality, 1)
        self.assertFalse(os.path.exists(quality.QUALITY_LIVE_ENV))
        self.assertFalse(os.path.exists(quality.QUALITY_ENV))

    def test_relay_online_allows_lower_quality(self):
        state = CameraState(quality=3)  # FHD
        live_calls = []
        persist_calls = []

        def live(raw):
            live_calls.append(raw)
            return quality_control.apply_live_quality(raw, state, lambda: 0)

        def persist(qenum):
            persist_calls.append(qenum)
            quality_control.persist_quality(qenum)

        ok = quality_control.handle_quality(
            5, True, live, persist, current_enum=state.quality, turn_online=True,
        )

        self.assertTrue(ok)
        self.assertEqual(live_calls, [5])
        self.assertEqual(persist_calls, [1])
        self.assertEqual(state.quality, 1)

    def test_relay_online_allows_equal_quality(self):
        state = CameraState(quality=2)  # HD
        ok = quality_control.handle_quality(
            6, False, lambda raw: quality_control.apply_live_quality(
                raw, state, lambda: 0
            ), lambda q: None, current_enum=state.quality, turn_online=True,
        )
        self.assertTrue(ok)
        self.assertEqual(state.quality, 2)

    def test_turn_offline_allows_raise(self):
        state = CameraState(quality=1)  # SD
        ok = quality_control.handle_quality(
            7, False, lambda raw: quality_control.apply_live_quality(
                raw, state, lambda: 0
            ), lambda q: None, current_enum=state.quality, turn_online=False,
        )
        self.assertTrue(ok)
        self.assertEqual(state.quality, 3)

    def test_legacy_call_without_current_tier_is_inert(self):
        state = CameraState(quality=1)
        ok = quality_control.handle_quality(
            7, False, lambda raw: quality_control.apply_live_quality(
                raw, state, lambda: 0
            ), lambda q: None,
        )
        self.assertTrue(ok)
        self.assertEqual(state.quality, 3)


class TurnOnlineStateTests(unittest.TestCase):
    def test_defaults_offline(self):
        self.assertFalse(CameraState().turn_online)




if __name__ == '__main__':
    unittest.main()
