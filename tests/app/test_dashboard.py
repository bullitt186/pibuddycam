"""WP-UI2 (AC-14/AC-17): dashboard aggregation and redaction.

Stdlib-only and hermetic. Every input is injected; no device, socket, or
``/data`` path is touched.
"""
import json
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import dashboard  # noqa: E402

NOW = 1_000_000.0


class FakeState:
    """Minimal CameraState-shaped object for the builder."""

    def __init__(self, **overrides):
        self.quality = 3
        self.camera_name = 'Buddy3D Camera'
        self.snapshot_interval = 30
        self.snapshot_upload_enabled = True
        self.timelapse_interval = 10
        self.timelapse_enabled = False
        self.timelapse_fps = 10
        self.rtsp_mode = 1
        self.rtsp_running = False
        self.webrtc_mode = 1
        self.webrtc_status = 1
        self.streaming = False
        self.turn_online = False
        for key, value in overrides.items():
            setattr(self, key, value)

    def resolution(self):
        return (1920, 1080)


def build(**overrides):
    kwargs = dict(
        now=NOW,
        settings={'quality_tier': 3, 'camera_name': 'Buddy3D Camera'},
        state=FakeState(),
        metrics={
            'storage_free_bytes': 5 * 1024 * 1024 * 1024,
            'wifi_rssi_dbm': -55,
            'cpu_temperature_c': 45.0,
            'uptime_seconds': 3661,
        },
        application_version='1.1.2',
        build_identity={'version': '1.1.2', 'source_commit': 'abc123'},
        signaling={'authenticated': True, 'connected': True},
        mqtt={'enabled': True, 'started': True, 'last_error': None},
        updates={
            'installed_version': '1.1.2',
            'latest_version': '1.1.2',
            'in_progress': False,
        },
        snapshot={'last_at': NOW - 5, 'ok': True,
                  'capture_at': NOW - 5, 'capture_ok': True},
    )
    kwargs.update(overrides)
    return dashboard.build_dashboard(**kwargs)


class ShapeTests(unittest.TestCase):
    def test_required_subsystems_present(self):
        payload = build()
        for key in (
            'ok', 'generated_at', 'runtime', 'version', 'settings', 'camera',
            'prusa', 'snapshots', 'rtsp', 'webrtc', 'mqtt', 'storage',
            'metrics', 'updates',
        ):
            self.assertIn(key, payload, key)

    def test_settings_projection_is_allowlisted(self):
        payload = build(settings={
            'quality_tier': 2,
            'camera_name': 'Cam',
            'token': 'should-not-appear',
            'password': 'should-not-appear',
        })
        self.assertEqual(payload['settings'], {
            'quality_tier': 2, 'camera_name': 'Cam'})
        self.assertNotIn('token', payload['settings'])
        self.assertNotIn('password', payload['settings'])

    def test_metrics_and_version_are_reported(self):
        payload = build()
        self.assertEqual(payload['metrics']['wifi_rssi_dbm'], -55)
        self.assertEqual(payload['metrics']['cpu_temperature_c'], 45.0)
        self.assertEqual(payload['metrics']['uptime_seconds'], 3661)
        self.assertEqual(payload['storage']['free_bytes'], 5 * 1024 ** 3)
        self.assertEqual(payload['version']['application'], '1.1.2')
        self.assertEqual(payload['version']['source_commit'], 'abc123')
        self.assertEqual(payload['camera']['resolution']['label'], '1920x1080')
        self.assertEqual(payload['camera']['quality']['name'], 'FHD')

    def test_build_identity_excludes_personal_paths(self):
        payload = build(build_identity={
            'version': '1.1.2',
            'package_manifest': '/home/secretuser/manifest.json',
        })
        body = json.dumps(payload)
        self.assertNotIn('package_manifest', payload['version'])
        self.assertNotIn('/home/secretuser', body)


class FreshnessTests(unittest.TestCase):
    def test_live_runtime_is_fresh(self):
        payload = build()
        self.assertEqual(payload['runtime']['source'], 'live')
        self.assertTrue(payload['runtime']['fresh'])

    def test_unavailable_runtime_is_not_fresh(self):
        payload = dashboard.unavailable_dashboard(NOW)
        self.assertEqual(payload['runtime']['source'], 'unavailable')
        self.assertFalse(payload['runtime']['fresh'])
        self.assertIsNone(payload['runtime']['observed_at'])

    def test_absent_observation_is_unknown_with_null_timestamp(self):
        payload = build(state=FakeState(), snapshot={}, metrics={},
                        signaling=None, mqtt=None, updates=None)
        for key in ('camera', 'prusa', 'snapshots', 'mqtt', 'storage',
                    'metrics', 'updates'):
            with self.subTest(subsystem=key):
                self.assertEqual(payload[key]['state'], 'unknown')
                self.assertIsNone(payload[key]['observed_at'])
                self.assertFalse(payload[key]['fresh'])

    def test_stale_snapshot_is_not_fresh(self):
        payload = build(snapshot={'last_at': NOW - 100000, 'ok': True})
        self.assertFalse(payload['snapshots']['fresh'])
        self.assertEqual(payload['snapshots']['age_seconds'], 100000.0)

    def test_camera_uses_capture_not_upload(self):
        # A failed upload must not report the camera source as errored.
        payload = build(snapshot={
            'last_at': NOW - 5, 'ok': False,
            'capture_at': NOW - 5, 'capture_ok': True,
        })
        self.assertEqual(payload['camera']['state'], 'running')
        self.assertEqual(payload['snapshots']['state'], 'failed')

    def test_camera_error_when_capture_fails(self):
        payload = build(snapshot={
            'last_at': NOW - 5, 'ok': False,
            'capture_at': NOW - 5, 'capture_ok': False,
        })
        self.assertEqual(payload['camera']['state'], 'error')

    def test_rtsp_distinguishes_configured_mode_from_runtime_state(self):
        payload = build(state=FakeState(rtsp_mode=2, rtsp_running=False))
        self.assertEqual(payload['rtsp']['configured'], 'enabled')
        self.assertEqual(payload['rtsp']['state'], 'stopped')

    def test_webrtc_distinguishes_configured_mode_from_streaming(self):
        payload = build(state=FakeState(webrtc_mode=1, streaming=True))
        self.assertEqual(payload['webrtc']['configured'], 'enabled')
        self.assertEqual(payload['webrtc']['state'], 'streaming')
        self.assertTrue(payload['webrtc']['streaming'])

    def test_prusa_reports_auth_and_signaling_separately(self):
        payload = build(signaling={'authenticated': False, 'connected': True})
        self.assertEqual(payload['prusa']['signaling'], 'connected')
        self.assertEqual(payload['prusa']['auth'], 'unauthenticated')

    def test_mqtt_enabled_but_not_started_is_unknown_not_connected(self):
        payload = build(mqtt={'enabled': True, 'started': False})
        self.assertEqual(payload['mqtt']['state'], 'unknown')
        self.assertTrue(payload['mqtt']['configured'])

    def test_updates_installing_state(self):
        payload = build(updates={
            'installed_version': '1.1.2', 'latest_version': '1.2.0',
            'in_progress': True,
        })
        self.assertEqual(payload['updates']['state'], 'installing')
        self.assertTrue(payload['updates']['in_progress'])


class RedactionTests(unittest.TestCase):
    def test_planted_state_secrets_are_absent(self):
        canaries = {
            'token': 'CANARY-TOKEN-abc',
            'fingerprint': 'CANARY-FINGERPRINT-def',
            'password': 'CANARY-PASSWORD-ghi',
            'psk': 'CANARY-PSK-jkl',
        }
        state = FakeState(**canaries)
        payload = build(state=state)
        body = json.dumps(payload)
        for value in canaries.values():
            self.assertNotIn(value, body, value)

    def test_literal_secrets_are_redacted_from_free_text(self):
        canary = 'CANARY-PASSWORD-xyz'
        payload = build(
            mqtt={'enabled': True, 'started': True,
                  'last_error': f'broker rejected {canary}'},
            secrets=(canary,),
        )
        body = json.dumps(payload)
        self.assertNotIn(canary, body)
        self.assertIn('redacted', payload['mqtt']['last_error'])

    def test_personal_path_is_scrubbed(self):
        payload = build(mqtt={
            'enabled': True, 'started': True,
            'last_error': '/home/secretuser/private/creds.toml missing',
        })
        self.assertNotIn('/home/secretuser', json.dumps(payload))
        self.assertIn('<path>', payload['mqtt']['last_error'])

    def test_traceback_collapses_to_error(self):
        payload = build(mqtt={
            'enabled': True, 'started': True,
            'last_error': 'Traceback (most recent call last):\n  File "x", line 1',
        })
        self.assertNotIn('Traceback', payload['mqtt']['last_error'])
        self.assertIn(payload['mqtt']['last_error'], ('error', 'command failed'))

    def test_redaction_is_recursive_and_bounded(self):
        payload = build(settings={'camera_name': 'x' * 5000})
        self.assertLessEqual(len(payload['settings']['camera_name']), 4096)


class ProviderTests(unittest.TestCase):
    class _FakeClient:
        def __init__(self, results):
            self._results = list(results)

        def dashboard(self):
            if self._results:
                return self._results.pop(0)
            return {'ok': False, 'error': 'runtime unavailable', 'degraded': True}

    def test_provider_returns_live_then_stale(self):
        clock = {'now': NOW}
        client = self._FakeClient([
            {'ok': True, 'data': build()},
            {'ok': False, 'error': 'gone', 'degraded': True},
        ])
        provider = dashboard.DashboardProvider(
            client, refresh_interval=1.0, stale_after=1.0,
            wall_clock=lambda: clock['now'])
        first = provider.snapshot()
        self.assertEqual(first['runtime']['source'], 'live')
        provider.refresh()  # fails; keeps last-known
        clock['now'] += 5
        second = provider.snapshot()
        self.assertEqual(second['runtime']['source'], 'stale')
        self.assertFalse(second['runtime']['fresh'])
        self.assertIn('gone', second['runtime'].get('reason', ''))

    def test_provider_degrades_when_never_connected(self):
        provider = dashboard.DashboardProvider(
            self._FakeClient([]), wall_clock=lambda: NOW)
        payload = provider.snapshot()
        self.assertEqual(payload['runtime']['source'], 'unavailable')

    def test_provider_does_not_mutate_its_cached_payload(self):
        payload = build()
        provider = dashboard.DashboardProvider(
            self._FakeClient([{'ok': True, 'data': payload}]),
            wall_clock=lambda: NOW)
        provider.snapshot()
        provider.snapshot()
        self.assertNotIn('fetched_at', payload['runtime'])

    def test_provider_never_raises_on_a_broken_client(self):
        class Boom:
            def dashboard(self):
                raise RuntimeError('boom')

        provider = dashboard.DashboardProvider(Boom(), wall_clock=lambda: NOW)
        payload = provider.snapshot()
        self.assertEqual(payload['runtime']['source'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
