"""Tests for GAP-DEVICE-01 (reboot) and GAP-DEVICE-02 (hardware truthfulness).

Stdlib-only: imports ``device_control``, ``state``, ``status`` and ``proto`` but
never ``socketio``/``aiohttp``/``gi``/GStreamer, and never touches systemd. The
reboot action is an injected callable, so these tests perform no real reboot.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))


import device_control  # noqa: E402
from proto import decode_message  # noqa: E402
from state import CameraState  # noqa: E402
from status import build_status_message  # noqa: E402


def nested(raw):
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    return decode_message(raw)


def recording_reboot(calls):
    def reboot():
        calls.append('reboot')
        return True
    return reboot


class RebootGuardTests(unittest.TestCase):
    def test_can_reboot_first_request_and_window_boundary(self):
        interval = device_control.DEFAULT_REBOOT_MIN_INTERVAL_SECONDS
        self.assertTrue(device_control.can_reboot(None, 1000.0, interval))
        self.assertTrue(device_control.can_reboot(1000.0, 1000.0 + interval, interval))
        self.assertFalse(device_control.can_reboot(1000.0, 1000.0 + interval - 0.001, interval))

    def test_backwards_clock_fails_closed(self):
        self.assertFalse(device_control.can_reboot(1000.0, 999.0, 60))

    def test_first_request_reboots_once_and_records_time(self):
        state = CameraState()
        calls = []
        self.assertTrue(
            device_control.request_reboot(state, recording_reboot(calls), now=500.0)
        )
        self.assertEqual(calls, ['reboot'])
        self.assertEqual(state.last_reboot_monotonic, 500.0)

    def test_second_request_inside_window_is_rejected_without_rebooting(self):
        state = CameraState()
        calls = []
        reboot = recording_reboot(calls)
        interval = device_control.DEFAULT_REBOOT_MIN_INTERVAL_SECONDS
        self.assertTrue(device_control.request_reboot(state, reboot, now=500.0))
        self.assertFalse(
            device_control.request_reboot(state, reboot, now=500.0 + interval - 1)
        )
        self.assertEqual(calls, ['reboot'])
        self.assertEqual(state.last_reboot_monotonic, 500.0)

    def test_request_after_window_is_allowed_again(self):
        state = CameraState()
        calls = []
        reboot = recording_reboot(calls)
        interval = device_control.DEFAULT_REBOOT_MIN_INTERVAL_SECONDS
        self.assertTrue(device_control.request_reboot(state, reboot, now=500.0))
        self.assertTrue(
            device_control.request_reboot(state, reboot, now=500.0 + interval)
        )
        self.assertEqual(calls, ['reboot', 'reboot'])

    def test_failing_reboot_fn_is_reported_and_does_not_crash(self):
        state = CameraState()

        def boom():
            raise RuntimeError('systemctl unavailable')

        self.assertFalse(device_control.request_reboot(state, boom, now=10.0))
        # The failed attempt is still recorded so a burst cannot hot-loop it.
        self.assertEqual(state.last_reboot_monotonic, 10.0)

    def test_reboot_fn_returning_false_is_not_success(self):
        state = CameraState()
        self.assertFalse(device_control.request_reboot(state, lambda: False, now=1.0))

    def test_default_now_uses_monotonic(self):
        state = CameraState()
        with mock.patch.object(device_control.time, 'monotonic', return_value=123.0):
            self.assertTrue(device_control.request_reboot(state, lambda: True))
        self.assertEqual(state.last_reboot_monotonic, 123.0)

    def test_guard_state_lives_on_the_shared_object(self):
        first = CameraState()
        second = CameraState()
        self.assertTrue(device_control.request_reboot(first, lambda: True, now=1.0))
        self.assertFalse(device_control.request_reboot(first, lambda: True, now=1.0))
        # A different state object has its own window.
        self.assertTrue(device_control.request_reboot(second, lambda: True, now=1.0))


class HardwareAvailabilityTests(unittest.TestCase):
    def test_camera_state_reports_all_hardware_unavailable(self):
        state = CameraState()
        self.assertFalse(state.ir_available)
        self.assertFalse(state.speaker_available)
        self.assertFalse(state.fan_available)
        self.assertFalse(state.microsd_available)
        self.assertIsNone(state.ir_mode)

    def test_light_control_mode_mapping_matches_fw_config(self):
        self.assertEqual(device_control.light_control_mode('auto'), 1)
        self.assertEqual(device_control.light_control_mode('day'), 2)
        self.assertEqual(device_control.light_control_mode('night'), 3)
        self.assertEqual(device_control.light_control_mode(' NIGHT '), 3)
        # The SIO configuration protobuf sends the mode as an integer.
        self.assertEqual(device_control.light_control_mode(1), 1)
        self.assertEqual(device_control.light_control_mode(2), 2)
        self.assertEqual(device_control.light_control_mode(3), 3)
        for invalid in ('off', '', 0, 9, None, True, b'auto', {'mode': 1}):
            with self.subTest(invalid=invalid):
                self.assertIsNone(device_control.light_control_mode(invalid))

    def test_light_control_never_claims_a_fake_applied_mode(self):
        state = CameraState()
        for value in ('auto', 'night', 'day'):
            with self.subTest(value=value):
                self.assertFalse(device_control.apply_light_control(value, state))
                self.assertIsNone(state.ir_mode)

    def test_light_control_rejects_unrecognized_values(self):
        state = CameraState()
        for value in (None, 1, '', 'flicker', {'mode': 1}):
            with self.subTest(value=value):
                self.assertFalse(device_control.apply_light_control(value, state))
        self.assertIsNone(state.ir_mode)

    def test_light_control_applies_only_when_hardware_reports_available(self):
        state = CameraState()
        state.ir_available = True
        self.assertTrue(device_control.apply_light_control('night', state))
        self.assertEqual(state.ir_mode, 3)

    def test_status_hardware_values_follow_the_recovered_descriptor(self):
        # camera_status (descriptor 0x3f6cd0): 3 = light mode (1 auto), 4 = snapshot
        # interval in seconds, 5 = upload state (1 uploading, 2 disabled), 6 = speaker
        # volume (firmware default 40). The Pi has neither IR light nor speaker and
        # keeps reporting the defaults; tags 1 and 2 are never populated.
        state = CameraState()
        top = decode_message(build_status_message(state, token='t', mac='m', ip='i'))
        camera_status = nested(top[3])
        self.assertEqual(sorted(camera_status), [3, 4, 5, 6])
        self.assertEqual(camera_status[3], 1)
        self.assertEqual(camera_status[4], state.snapshot_interval)
        self.assertEqual(camera_status[5], 1)
        self.assertEqual(camera_status[6], 40)

    def test_status_reports_a_disabled_snapshot_upload(self):
        state = CameraState()
        state.snapshot_upload_enabled = False
        top = decode_message(build_status_message(state, token='t', mac='m', ip='i'))
        self.assertEqual(nested(top[3])[5], 2)
        state.snapshot_upload_enabled = True
        top = decode_message(build_status_message(state, token='t', mac='m', ip='i'))
        self.assertEqual(nested(top[3])[5], 1)

    def test_status_carries_no_brightness_value(self):
        # No camera_status tag is a brightness: the firmware reports only the light
        # mode, never a level. Pin the tag set so one is not added by guesswork.
        state = CameraState()
        top = decode_message(build_status_message(state, token='t', mac='m', ip='i'))
        self.assertLessEqual(set(nested(top[3])), {3, 4, 5, 6})


if __name__ == '__main__':
    unittest.main()
