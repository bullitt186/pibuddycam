"""Media-section ids on the wire: Connect's sdpMid "0" must match the offer."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'app'))

import sdp_mids  # noqa: E402

OFFER = (
    'v=0\r\no=- 1 0 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n'
    'a=ice-options:trickle\r\na=group:BUNDLE video0\r\n'
    'm=video 9 UDP/TLS/RTP/SAVPF 96\r\nc=IN IP4 0.0.0.0\r\n'
    'a=setup:actpass\r\na=rtpmap:96 H264/90000\r\na=mid:video0\r\n'
    'a=fingerprint:sha-256 AA\r\n'
)
ANSWER = (
    'v=0\r\no=- 2 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n'
    'a=group:BUNDLE 0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n'
    'a=mid:0\r\na=recvonly\r\n'
)


class WireMidTests(unittest.TestCase):
    def test_the_offer_leaves_with_mid_zero_in_the_media_and_group_lines(self):
        wire, mapping = sdp_mids.to_wire(OFFER)
        self.assertEqual(mapping, {'video0': '0'})
        self.assertIn('a=mid:0\r\n', wire)
        self.assertIn('a=group:BUNDLE 0\r\n', wire)
        self.assertNotIn('video0', wire)
        self.assertEqual(sdp_mids.mids(wire), ['0'])

    def test_only_the_ids_change_and_the_line_endings_stay(self):
        wire, _ = sdp_mids.to_wire(OFFER)
        self.assertEqual(wire.replace('a=mid:0', 'a=mid:video0')
                         .replace('BUNDLE 0', 'BUNDLE video0'), OFFER)
        self.assertNotIn('\n', wire.replace('\r\n', ''))

    def test_the_answer_is_mapped_back_for_webrtcbin(self):
        _wire, mapping = sdp_mids.to_wire(OFFER)
        back = sdp_mids.from_wire(ANSWER, mapping)
        self.assertEqual(sdp_mids.mids(back), ['video0'])
        self.assertIn('a=group:BUNDLE video0\r\n', back)
        self.assertIn('a=recvonly\r\n', back)

    def test_ids_that_already_are_indices_are_left_alone(self):
        wire, mapping = sdp_mids.to_wire(ANSWER)
        self.assertEqual(mapping, {})
        self.assertEqual(wire, ANSWER)
        self.assertEqual(sdp_mids.from_wire(ANSWER, {}), ANSWER)

    def test_two_sections_are_numbered_by_position(self):
        sdp = (
            'a=group:BUNDLE audio0 video0\r\nm=audio 9 X 0\r\na=mid:audio0\r\n'
            'm=video 9 X 96\r\na=mid:video0\r\n'
        )
        wire, mapping = sdp_mids.to_wire(sdp)
        self.assertEqual(mapping, {'audio0': '0', 'video0': '1'})
        self.assertEqual(sdp_mids.mids(wire), ['0', '1'])
        self.assertIn('a=group:BUNDLE 0 1', wire)
        self.assertEqual(sdp_mids.from_wire(wire, mapping), sdp)

    def test_swapped_ids_do_not_collide(self):
        sdp = 'a=group:BUNDLE 1 0\r\nm=a\r\na=mid:1\r\nm=b\r\na=mid:0\r\n'
        wire, mapping = sdp_mids.to_wire(sdp)
        self.assertEqual(sdp_mids.mids(wire), ['0', '1'])
        self.assertEqual(sdp_mids.from_wire(wire, mapping), sdp)

    def test_text_without_a_mid_is_unchanged(self):
        wire, mapping = sdp_mids.to_wire('v=0\r\ns=-\r\n')
        self.assertEqual((wire, mapping), ('v=0\r\ns=-\r\n', {}))
        self.assertEqual(sdp_mids.mids(None), [])


if __name__ == '__main__':
    unittest.main()
