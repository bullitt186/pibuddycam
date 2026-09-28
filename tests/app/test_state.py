import os
import sys
import tempfile
import unittest
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import quality  # noqa: E402
from state import (  # noqa: E402
    DEFAULT_QUALITY,
    ENUM_TO_RAW,
    RAW_TO_ENUM,
    RESOLUTIONS,
    SNAPSHOT_INTERVAL_MAX,
    SNAPSHOT_INTERVAL_MIN,
    CameraState,
    snapshot_interval_from_config,
)


class QualityMappingTests(unittest.TestCase):
    def test_raw_bytes_map_to_firmware_enums(self):
        # GAP-QUALITY-01: raw 5=SD, 6=HD, 7=FHD.
        self.assertEqual(RAW_TO_ENUM, {5: 1, 6: 2, 7: 3})
        self.assertEqual(ENUM_TO_RAW, {1: 5, 2: 6, 3: 7})

    def test_enum_resolutions(self):
        self.assertEqual(RESOLUTIONS, {1: (640, 480), 2: (1280, 720), 3: (1920, 1080)})

    def test_state_resolution_tracks_quality(self):
        state = CameraState()
        for raw, expected_res in ((5, (640, 480)), (6, (1280, 720)), (7, (1920, 1080))):
            enum = RAW_TO_ENUM[raw]
            self.assertTrue(state.set_quality(enum))
            self.assertEqual(state.resolution(), expected_res)

    def test_set_quality_validates_enum_range(self):
        state = CameraState()
        for invalid in (0, 4, -1, '2', 2.0, None, True):
            with self.subTest(invalid=invalid):
                self.assertFalse(state.set_quality(invalid))
        self.assertTrue(state.set_quality(2))
        self.assertEqual(state.quality, 2)


class SnapshotIntervalTests(unittest.TestCase):
    def test_accepts_inclusive_range_boundaries(self):
        state = CameraState()
        for seconds in (SNAPSHOT_INTERVAL_MIN, 30, SNAPSHOT_INTERVAL_MAX):
            with self.subTest(seconds=seconds):
                self.assertTrue(state.set_snapshot_interval(seconds))
                self.assertEqual(state.snapshot_interval, seconds)

    def test_rejects_out_of_range_and_non_int(self):
        state = CameraState()
        state.set_snapshot_interval(30)
        for invalid in (9, 601, 0, -10, '30', 30.0, 30.5, None, True):
            with self.subTest(invalid=invalid):
                self.assertFalse(state.set_snapshot_interval(invalid))
                self.assertEqual(state.snapshot_interval, 30)

    def test_valid_change_wakes_waiting_loop(self):
        state = CameraState()
        self.assertFalse(state.snapshot_interval_changed.is_set())
        self.assertTrue(state.set_snapshot_interval(60))
        self.assertTrue(state.snapshot_interval_changed.is_set())

    def test_rejected_change_does_not_wake_loop(self):
        state = CameraState()
        self.assertFalse(state.set_snapshot_interval(9))
        self.assertFalse(state.snapshot_interval_changed.is_set())


class SnapshotIntervalConfigTests(unittest.TestCase):
    """Review fix 3: config upload.interval is validated to 10..600."""

    def test_accepts_in_range_integers(self):
        for raw in ('10', '30', '600', 10, 600):
            with self.subTest(raw=raw):
                self.assertEqual(snapshot_interval_from_config(raw), int(raw))

    def test_rejects_out_of_range_and_malformed(self):
        for raw in ('9', '601', '0', '-5', '', 'abc', '10.5', None, 9, 601, [10]):
            with self.subTest(raw=raw):
                self.assertIsNone(snapshot_interval_from_config(raw))


class TimelapseIntervalTests(unittest.TestCase):
    """GAP-CONFIG-01: config field 2 = set_timelaps_interval (FUN_000a7940)."""

    def test_accepts_timelapse_module_range(self):
        state = CameraState()
        for seconds in (1, 30, 3600):
            with self.subTest(seconds=seconds):
                self.assertTrue(state.set_timelapse_interval(seconds))
                self.assertEqual(state.timelapse_interval, seconds)

    def test_rejects_out_of_range_and_non_int(self):
        state = CameraState()
        state.set_timelapse_interval(30)
        for invalid in (0, 3601, -1, '30', 30.0, 30.5, None, True):
            with self.subTest(invalid=invalid):
                self.assertFalse(state.set_timelapse_interval(invalid))
                self.assertEqual(state.timelapse_interval, 30)


class CameraNameTests(unittest.TestCase):
    def test_accepts_non_empty_and_strips(self):
        state = CameraState()
        self.assertTrue(state.set_camera_name('  Print Room  '))
        self.assertEqual(state.camera_name, 'Print Room')

    def test_rejects_empty_whitespace_and_non_string(self):
        state = CameraState()
        for invalid in ('', '   ', '\t\n', None, 42, b'name'):
            with self.subTest(invalid=invalid):
                self.assertFalse(state.set_camera_name(invalid))
        self.assertEqual(state.camera_name, 'PiBuddyCam')


class CameraStateDefaultsTests(unittest.TestCase):
    def test_defaults(self):
        state = CameraState()
        self.assertEqual(state.camera_name, 'PiBuddyCam')
        self.assertEqual(state.quality, DEFAULT_QUALITY)
        self.assertEqual(state.snapshot_interval, SNAPSHOT_INTERVAL_MIN)
        self.assertTrue(state.snapshot_upload_enabled)
        self.assertEqual(state.rtsp_mode, 1)
        self.assertFalse(state.rtsp_running)
        self.assertEqual(state.webrtc_mode, 1)
        self.assertEqual(state.webrtc_status, 1)
        self.assertFalse(state.streaming)
        self.assertTrue(state.info_dirty)

    def test_mark_info_dirty(self):
        state = CameraState()
        state.info_dirty = False
        state.mark_info_dirty()
        self.assertTrue(state.info_dirty)


class QualityPersistenceTests(unittest.TestCase):
    """GAP-QUALITY-02/03: persisted vs live tier files feed the shared state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_envs = (quality.QUALITY_ENV, quality.QUALITY_LIVE_ENV)
        quality.QUALITY_ENV = os.path.join(self._tmp.name, 'quality.env')
        quality.QUALITY_LIVE_ENV = os.path.join(self._tmp.name, 'quality.live.env')

    def tearDown(self):
        quality.QUALITY_ENV, quality.QUALITY_LIVE_ENV = self._old_envs
        self._tmp.cleanup()

    def test_persisted_tier_round_trips_into_state_resolution(self):
        for enum, expected in RESOLUTIONS.items():
            with self.subTest(enum=enum):
                self.assertEqual(quality.write_current(enum), expected)
                loaded_enum, _, _ = quality.read_current()
                state = CameraState()
                state.set_quality(loaded_enum)
                self.assertEqual(state.resolution(), expected)

    def test_live_override_wins_and_leaves_persisted_tier_untouched(self):
        quality.write_current(3)   # FHD persisted
        quality.write_live(1)      # SD live
        self.assertEqual(quality.read_current(), (1, 640, 480))
        with open(quality.QUALITY_ENV) as f:
            self.assertIn('CAM_WIDTH=1920', f.read())

    def test_live_override_cleared_falls_back_to_persisted(self):
        quality.write_current(2)   # HD persisted
        quality.write_live(3)      # FHD live
        os.remove(quality.QUALITY_LIVE_ENV)
        self.assertEqual(quality.read_current(), (2, 1280, 720))


class PersistedStateTests(unittest.TestCase):
    """GAP-PERSIST-01: the durable subset of CameraState."""

    def test_persistable_state_contents(self):
        state = CameraState()
        state.set_quality(2)
        state.set_camera_name('  Print Room  ')
        state.set_snapshot_interval(30)
        state.snapshot_upload_enabled = False
        state.set_timelapse_interval(15)
        state.timelapse_enabled = True
        state.timelapse_fps = 12
        state.rtsp_mode = 2
        state.webrtc_mode = 0
        self.assertEqual(state.persistable_state(), {
            'quality_tier': 2,
            'rotation': 0,
            'camera_name': 'Print Room',
            'snapshot_interval': 30,
            'snapshot_upload_enabled': False,
            'timelapse_interval': 15,
            'timelapse_enabled': True,
            'timelapse_fps': 12,
            'timelapse_trigger': 'interval',
            'timelapse_gpio_pin': None,
            'timelapse_gpio_record_pin': None,
            'rtsp_mode': 2,
            'webrtc_mode': 0,
        })
        # The store version is owned by settings_store.save, not CameraState.
        self.assertNotIn('version', state.persistable_state())

    def test_gpio_trigger_settings_validation(self):
        state = CameraState()
        self.assertEqual(state.timelapse_trigger, 'interval')
        # gpio needs a layer pin first
        self.assertFalse(state.set_timelapse_trigger('gpio'))
        for bad in (0, 1, 2, 3, 7, 11, 14, 15, 99, True, '17', 17.0):
            self.assertFalse(state.set_timelapse_gpio_pin(bad), bad)
        self.assertTrue(state.set_timelapse_gpio_pin(17))
        self.assertFalse(state.set_timelapse_gpio_record_pin(17))
        self.assertTrue(state.set_timelapse_gpio_record_pin(27))
        self.assertFalse(state.set_timelapse_gpio_pin(27))
        self.assertTrue(state.set_timelapse_trigger('gpio'))
        self.assertFalse(state.set_timelapse_trigger('sometimes'))
        # the layer pin cannot be cleared while the trigger is gpio
        self.assertFalse(state.set_timelapse_gpio_pin(None))
        self.assertTrue(state.set_timelapse_trigger('interval'))
        self.assertTrue(state.set_timelapse_gpio_pin(None))
        self.assertTrue(state.set_timelapse_gpio_record_pin(None))

    def test_gpio_trigger_settings_round_trip(self):
        state = CameraState()
        applied = state.apply_persisted({
            'timelapse_trigger': 'gpio', 'timelapse_gpio_pin': 17,
            'timelapse_gpio_record_pin': 27,
        })
        self.assertEqual(
            applied,
            ['timelapse_gpio_pin', 'timelapse_gpio_record_pin', 'timelapse_trigger'])
        persisted = state.persistable_state()
        fresh = CameraState()
        fresh.apply_persisted(persisted)
        self.assertEqual(fresh.timelapse_trigger, 'gpio')
        self.assertEqual(fresh.timelapse_gpio_pin, 17)
        self.assertEqual(fresh.timelapse_gpio_record_pin, 27)

    def test_persisted_gpio_trigger_without_pin_falls_back_to_interval(self):
        state = CameraState()
        state.apply_persisted({'timelapse_trigger': 'gpio', 'timelapse_gpio_pin': 3})
        self.assertEqual(state.timelapse_trigger, 'interval')
        self.assertIsNone(state.timelapse_gpio_pin)

    def test_rotation_default_validation_and_round_trip(self):
        state = CameraState()
        self.assertEqual(state.rotation, 0)
        for bad in (45, '90', 90.0, True, None):
            self.assertFalse(state.set_rotation(bad), bad)
        self.assertEqual(state.rotation, 0)
        self.assertTrue(state.set_rotation(270))
        restored = CameraState()
        self.assertEqual(restored.apply_persisted(state.persistable_state()).count('rotation'), 1)
        self.assertEqual(restored.rotation, 270)

    def test_missing_or_invalid_persisted_rotation_keeps_default(self):
        for data in ({}, {'rotation': 45}, {'rotation': '90'}):
            with self.subTest(data=data):
                state = CameraState()
                self.assertNotIn('rotation', state.apply_persisted(data))
                self.assertEqual(state.rotation, 0)

    def test_oriented_resolution_swaps_for_90_and_270(self):
        state = CameraState()
        state.set_quality(2)
        for degrees, expected in ((0, (1280, 720)), (90, (720, 1280)),
                                  (180, (1280, 720)), (270, (720, 1280))):
            with self.subTest(degrees=degrees):
                state.set_rotation(degrees)
                self.assertEqual(state.oriented_resolution(), expected)
                # The encoder tier size itself never changes with rotation.
                self.assertEqual(state.resolution(), (1280, 720))

    def test_rotation_change_republishes_info(self):
        state = CameraState()
        state.info_dirty = False
        state.set_rotation(0)
        self.assertFalse(state.info_dirty)
        state.set_rotation(90)
        self.assertTrue(state.info_dirty)

    def test_apply_persisted_applies_valid_keys(self):
        state = CameraState()
        applied = state.apply_persisted({
            'quality_tier': 1,
            'camera_name': 'Workshop',
            'snapshot_interval': 600,
            'snapshot_upload_enabled': False,
            'timelapse_interval': 3600,
            'timelapse_enabled': True,
            'timelapse_fps': 30,
            'rtsp_mode': 2,
            'webrtc_mode': 0,
        })
        self.assertEqual(set(applied), {
            'quality_tier', 'camera_name', 'snapshot_interval',
            'snapshot_upload_enabled', 'timelapse_interval', 'timelapse_enabled',
            'timelapse_fps', 'rtsp_mode', 'webrtc_mode',
        })
        self.assertEqual(state.quality, 1)
        self.assertEqual(state.camera_name, 'Workshop')
        self.assertEqual(state.snapshot_interval, 600)
        self.assertFalse(state.snapshot_upload_enabled)
        self.assertEqual(state.timelapse_interval, 3600)
        self.assertTrue(state.timelapse_enabled)
        self.assertEqual(state.timelapse_fps, 30)
        self.assertEqual(state.rtsp_mode, 2)
        self.assertEqual(state.webrtc_mode, 0)

    def test_apply_persisted_ignores_invalid_and_unknown(self):
        state = CameraState()
        applied = state.apply_persisted({
            'quality_tier': 9,
            'camera_name': '   ',
            'snapshot_interval': 5,
            'snapshot_upload_enabled': 'yes',
            'timelapse_interval': 0,
            'timelapse_enabled': 1,
            'timelapse_fps': 99,
            'rtsp_mode': 3,
            'webrtc_mode': 7,
            'version': 1,
            'unknown_key': 'ignored',
        })
        self.assertEqual(applied, [])
        self.assertEqual(state.quality, DEFAULT_QUALITY)
        self.assertEqual(state.camera_name, 'PiBuddyCam')
        self.assertEqual(state.snapshot_interval, SNAPSHOT_INTERVAL_MIN)
        self.assertTrue(state.snapshot_upload_enabled)
        self.assertEqual(state.timelapse_interval, 10)
        self.assertFalse(state.timelapse_enabled)
        self.assertEqual(state.timelapse_fps, 10)
        self.assertEqual(state.rtsp_mode, 1)
        self.assertEqual(state.webrtc_mode, 1)

    def test_apply_persisted_never_raises_on_bad_data(self):
        state = CameraState()
        for bad in (None, [], 'nope', 42, {'quality_tier': None}):
            with self.subTest(bad=bad):
                self.assertEqual(state.apply_persisted(bad), [])


class SnapshotUploadPredicateTests(unittest.TestCase):
    def test_periodic_upload_allowed_by_default(self):
        state = CameraState()
        self.assertTrue(state.periodic_snapshot_allowed(rtsp_active=False))

    def test_disabled_upload_pauses_periodic_loop(self):
        state = CameraState()
        state.snapshot_upload_enabled = False
        self.assertFalse(state.periodic_snapshot_allowed())
        state.snapshot_upload_enabled = True
        self.assertTrue(state.periodic_snapshot_allowed())

    def test_active_streams_do_not_pause_shared_mux_capture(self):
        state = CameraState()
        self.assertTrue(state.periodic_snapshot_allowed(rtsp_active=True))
        state.streaming = True
        self.assertTrue(state.periodic_snapshot_allowed())


if __name__ == '__main__':
    unittest.main()
