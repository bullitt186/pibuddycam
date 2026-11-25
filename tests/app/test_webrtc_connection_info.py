"""GAP-WEBRTC-06: `webrtc_connection_info` candidate mapping and wiring.

The pure helpers live in ``webrtc_lifecycle`` and are imported directly. The
``signaling`` sender is exercised with a minimal in-process double so the
socketio runtime dependency is never loaded. ``main.py`` and ``webrtc.py``
depend on aiohttp/PyGObject respectively, so their wiring is checked with the
AST pattern used by ``test_webrtc_lifecycle``.
"""
import asyncio
import sys
import types
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

# signaling imports socketio; inject a minimal double (tests must not import it).
_socketio_stub = types.ModuleType('socketio')
_socketio_stub.AsyncClient = object
sys.modules.setdefault('socketio', _socketio_stub)

import signaling  # noqa: E402
import webrtc_lifecycle  # noqa: E402
from proto import decode_message, encode_message  # noqa: E402


class CandidateTypeCodeTests(unittest.TestCase):
    def test_confirmed_mapping(self):
        self.assertEqual(webrtc_lifecycle.candidate_type_code('host'), 1)
        self.assertEqual(webrtc_lifecycle.candidate_type_code('srflx'), 2)
        self.assertEqual(webrtc_lifecycle.candidate_type_code('prflx'), 3)
        self.assertEqual(webrtc_lifecycle.candidate_type_code('relay'), 4)
        self.assertEqual(webrtc_lifecycle.candidate_type_code('undefined'), 5)
        self.assertEqual(webrtc_lifecycle.candidate_type_code('unknown'), 0)

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(webrtc_lifecycle.candidate_type_code(' HOST '), 1)
        self.assertEqual(webrtc_lifecycle.candidate_type_code('Relay'), 4)

    def test_none_and_unrecognized_map_to_unknown(self):
        self.assertEqual(webrtc_lifecycle.candidate_type_code(None), 0)
        self.assertEqual(webrtc_lifecycle.candidate_type_code(''), 0)
        self.assertEqual(webrtc_lifecycle.candidate_type_code('bogus'), 0)
        self.assertEqual(webrtc_lifecycle.candidate_type_code(7), 0)

    def test_no_pair_code_is_six(self):
        self.assertEqual(webrtc_lifecycle.CANDIDATE_TYPE_NO_PAIR, 6)


class ParseCandidateTypeTests(unittest.TestCase):
    def test_full_candidate_line(self):
        line = ('candidate:1 1 UDP 2122252543 192.0.2.10 50000 '
                'typ host')
        self.assertEqual(webrtc_lifecycle.parse_candidate_type(line), 'host')

    def test_a_equals_prefixed_and_uppercase(self):
        line = 'a=candidate:2 1 TCP 2105 203.0.113.5 9 typ SRFLX raddr 0.0.0.0'
        self.assertEqual(webrtc_lifecycle.parse_candidate_type(line), 'srflx')

    def test_relay_and_prflx(self):
        self.assertEqual(
            webrtc_lifecycle.parse_candidate_type('x typ relay raddr 1.2.3.4'),
            'relay',
        )
        self.assertEqual(
            webrtc_lifecycle.parse_candidate_type('x typ prflx'), 'prflx'
        )

    def test_missing_or_invalid(self):
        self.assertIsNone(webrtc_lifecycle.parse_candidate_type(None))
        self.assertIsNone(webrtc_lifecycle.parse_candidate_type(''))
        self.assertIsNone(webrtc_lifecycle.parse_candidate_type('no type token'))
        self.assertIsNone(webrtc_lifecycle.parse_candidate_type(b'typ host'))


class ConnectionInfoPayloadTests(unittest.TestCase):
    def test_payload_uses_fields_one_two_three_only(self):
        payload = webrtc_lifecycle.connection_info_payload('client-1', 1, 4)
        self.assertEqual(payload, {1: 'client-1', 2: 1, 3: 4})
        self.assertNotIn(4, payload)
        self.assertNotIn(5, payload)
        self.assertNotIn(6, payload)

    def test_non_string_client_id_coerced_to_empty(self):
        self.assertEqual(
            webrtc_lifecycle.connection_info_payload(None, 0, 0)[1], ''
        )

    def test_payload_encodes_as_numeric_bytes(self):
        # The wire values for fields 2/3 are uvarints, not strings
        # (protocol.md corrected 2026-09-20).
        msg = encode_message(
            webrtc_lifecycle.connection_info_payload('c', 2, 3)
        )
        fields = decode_message(msg)
        self.assertEqual(fields, {1: 'c', 2: 2, 3: 3})


class _FakeSender:
    """Minimal stand-in providing just what the sender method uses."""

    def __init__(self):
        self.sent = []

    async def sio_emit(self, event, data, callback=None):
        self.sent.append((event, data, callback))

    def _log_ack(self, event):
        return ('ack', event)


class SendConnectionInfoTests(unittest.TestCase):
    def _send(self, client_id, local_code, remote_code):
        sender = _FakeSender()
        asyncio.run(
            signaling.PrusaSignaling.send_webrtc_connection_info(
                sender, client_id, local_code, remote_code
            )
        )
        return sender

    def test_event_and_field_map(self):
        sender = self._send('client-1', 1, 4)
        self.assertEqual(len(sender.sent), 1)
        event, data, callback = sender.sent[0]
        self.assertEqual(event, 'webrtc_connection_info')
        self.assertEqual(decode_message(data), {1: 'client-1', 2: 1, 3: 4})
        self.assertIsNotNone(callback)

    def test_fields_four_five_six_omitted(self):
        sender = self._send('client-1', 0, 6)
        fields = decode_message(sender.sent[0][1])
        self.assertNotIn(4, fields)
        self.assertNotIn(5, fields)
        self.assertNotIn(6, fields)




if __name__ == '__main__':
    unittest.main()
