"""Local WebRTC signaling policy: viewer cap and client-message validation.

Stdlib-only and hermetic: no GStreamer, no network, no subprocess. Mirrors the
testing conventions of tests/test_pi_live_monitor.py.
"""
import json
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import local_webrtc_signaling as sig  # noqa: E402


class ViewerRegistryTests(unittest.TestCase):
    def test_default_cap_is_one(self):
        self.assertEqual(sig.DEFAULT_MAX_VIEWERS, 1)
        registry = sig.ViewerRegistry()
        self.assertTrue(registry.try_acquire())
        self.assertFalse(registry.try_acquire())

    def test_release_frees_a_slot(self):
        registry = sig.ViewerRegistry(max_viewers=1)
        self.assertTrue(registry.try_acquire())
        self.assertFalse(registry.try_acquire())
        registry.release()
        self.assertTrue(registry.try_acquire())

    def test_release_without_a_held_slot_never_goes_negative(self):
        registry = sig.ViewerRegistry(max_viewers=1)
        registry.release()
        registry.release()
        self.assertEqual(registry.metrics()['active'], 0)
        self.assertTrue(registry.try_acquire())

    def test_higher_cap_allows_more_concurrent_viewers(self):
        registry = sig.ViewerRegistry(max_viewers=2)
        self.assertTrue(registry.try_acquire())
        self.assertTrue(registry.try_acquire())
        self.assertFalse(registry.try_acquire())

    def test_metrics_are_bounded_and_secret_free(self):
        registry = sig.ViewerRegistry(max_viewers=3)
        registry.try_acquire()
        metrics = registry.metrics()
        blob = json.dumps(metrics, sort_keys=True)
        for canary in ('/data', 'token', 'password', 'secret', 'sdp'):
            self.assertNotIn(canary, blob, canary)
        self.assertEqual(metrics, {'active': 1, 'cap': 3})

    def test_invalid_max_viewers_rejected(self):
        with self.assertRaises(ValueError):
            sig.ViewerRegistry(max_viewers=0)
        with self.assertRaises(ValueError):
            sig.ViewerRegistry(max_viewers=-1)


class ParseClientMessageTests(unittest.TestCase):
    def test_valid_answer(self):
        msg_type, payload = sig.parse_client_message(
            json.dumps({'type': 'answer', 'sdp': 'v=0...'}))
        self.assertEqual(msg_type, 'answer')
        self.assertEqual(payload, {'sdp': 'v=0...'})

    def test_valid_ice_with_default_mline(self):
        msg_type, payload = sig.parse_client_message(
            json.dumps({'type': 'ice', 'candidate': 'candidate:1 1 UDP...'}))
        self.assertEqual(msg_type, 'ice')
        self.assertEqual(payload, {'candidate': 'candidate:1 1 UDP...', 'sdpMLineIndex': 0})

    def test_valid_ice_with_explicit_mline(self):
        msg_type, payload = sig.parse_client_message(json.dumps(
            {'type': 'ice', 'candidate': 'candidate:1 1 UDP...', 'sdpMLineIndex': 2}))
        self.assertEqual(payload['sdpMLineIndex'], 2)

    def test_valid_stop_has_no_payload_fields(self):
        msg_type, payload = sig.parse_client_message(json.dumps({'type': 'stop'}))
        self.assertEqual(msg_type, 'stop')
        self.assertEqual(payload, {})

    def test_bytes_input_is_decoded(self):
        msg_type, _ = sig.parse_client_message(
            json.dumps({'type': 'stop'}).encode('utf-8'))
        self.assertEqual(msg_type, 'stop')

    def test_non_utf8_bytes_rejected(self):
        with self.assertRaises(sig.SignalingError):
            sig.parse_client_message(b'\xff\xfe\x00\x01')

    def test_non_text_input_rejected(self):
        for bad in (None, 123, [], {}):
            with self.subTest(bad=bad):
                with self.assertRaises(sig.SignalingError):
                    sig.parse_client_message(bad)

    def test_empty_and_oversized_payload_rejected(self):
        with self.assertRaises(sig.SignalingError):
            sig.parse_client_message('')
        oversized = json.dumps({'type': 'answer', 'sdp': 'x' * sig.MAX_SDP_BYTES})
        with self.assertRaises(sig.SignalingError):
            sig.parse_client_message(oversized)

    def test_invalid_json_rejected(self):
        with self.assertRaises(sig.SignalingError):
            sig.parse_client_message('not json')

    def test_non_object_json_rejected(self):
        for bad in ('[]', '"a string"', '42', 'null'):
            with self.subTest(bad=bad):
                with self.assertRaises(sig.SignalingError):
                    sig.parse_client_message(bad)

    def test_unrecognized_type_rejected(self):
        with self.assertRaises(sig.SignalingError):
            sig.parse_client_message(json.dumps({'type': 'offer', 'sdp': 'x'}))
        with self.assertRaises(sig.SignalingError):
            sig.parse_client_message(json.dumps({}))

    def test_answer_missing_or_invalid_sdp_rejected(self):
        for bad_sdp in (None, '', 123, []):
            with self.subTest(bad_sdp=bad_sdp):
                with self.assertRaises(sig.SignalingError):
                    sig.parse_client_message(json.dumps({'type': 'answer', 'sdp': bad_sdp}))

    def test_answer_oversized_sdp_rejected(self):
        with self.assertRaises(sig.SignalingError):
            sig.parse_client_message(json.dumps(
                {'type': 'answer', 'sdp': 'x' * (sig.MAX_SDP_BYTES + 1)}))

    def test_ice_missing_or_invalid_candidate_rejected(self):
        for bad_candidate in (None, 123, []):
            with self.subTest(bad_candidate=bad_candidate):
                with self.assertRaises(sig.SignalingError):
                    sig.parse_client_message(
                        json.dumps({'type': 'ice', 'candidate': bad_candidate}))

    def test_ice_oversized_candidate_rejected(self):
        with self.assertRaises(sig.SignalingError):
            sig.parse_client_message(json.dumps(
                {'type': 'ice', 'candidate': 'x' * (sig.MAX_CANDIDATE_BYTES + 1)}))

    def test_ice_invalid_mline_rejected(self):
        for bad_mline in (-1, 'zero', 1.5, True, False):
            with self.subTest(bad_mline=bad_mline):
                with self.assertRaises(sig.SignalingError):
                    sig.parse_client_message(json.dumps(
                        {'type': 'ice', 'candidate': 'candidate:1', 'sdpMLineIndex': bad_mline}))


if __name__ == '__main__':
    unittest.main()
