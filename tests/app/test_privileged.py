"""WP-R1 B2: fixed-verb privileged client (privileged).

Host-only: every case injects a fake ``runner``; the import-safety test patches
``subprocess.run`` so no real ``sudo``/helper runs. The PSK is asserted never to
appear in argv or a failure reason.
"""
import importlib
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import hotspot  # noqa: E402
import privileged  # noqa: E402

PSK = 'sup3rsecret'


class FakeResult:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_runner(handlers=(), default=None, timeout_match=None, unavailable_match=None):
    calls = []

    def runner(args, timeout, input=None):
        calls.append((list(args), timeout, input))
        joined = ' '.join(args)
        if unavailable_match and unavailable_match in joined:
            raise FileNotFoundError(2, 'No such file or directory')
        if timeout_match and timeout_match in joined:
            raise subprocess.TimeoutExpired(args, timeout)
        for needle, result in handlers:
            if needle in joined:
                return result
        return default if default is not None else FakeResult(0, '')

    runner.calls = calls
    return runner


def all_argv(runner):
    return [arg for args, _timeout, _stdin in runner.calls for arg in args]


class ImportSafetyTests(unittest.TestCase):
    def test_import_does_not_execute_subprocess(self):
        with patch.object(
            subprocess, 'run', side_effect=AssertionError('subprocess on import')
        ):
            importlib.reload(privileged)
        self.assertTrue(callable(privileged.start_camera))
        self.assertTrue(callable(privileged.activate_station))
        self.assertTrue(callable(privileged.install_update))


class AllowlistTests(unittest.TestCase):
    def test_verbs_are_exactly_the_helper_verbs(self):
        self.assertEqual(
            privileged.VERBS,
            frozenset({
                'start-camera', 'stop-provisioning', 'hotspot-start',
                'hotspot-stop', 'wifi-station-apply', 'install-update',
                'check-update', 'reboot',
                'rtsp-start', 'rtsp-stop', 'quality-restart', 'camera-restart',
                'network-apply', 'hostname-apply', 'wifi-scan', 'ntp-apply',
                'timezone-apply',
            }),
        )

    def test_rtsp_verbs_match_the_helper(self):
        # Every Python verb must be a case label in the root helper.
        helper = (
            Path(__file__).resolve().parents[2]
            / 'image'
            / 'assets'
            / 'pibuddycam-priv'
        ).read_text(encoding='utf-8')
        for verb in ('rtsp-start', 'rtsp-stop', 'quality-restart',
                     'camera-restart', 'check-update', 'reboot'):
            self.assertIn(f'{verb})', helper)

    def test_unknown_verb_is_rejected_without_running(self):
        runner = make_runner()
        result = privileged._invoke('rm-rf', runner=runner)
        self.assertFalse(result)
        self.assertEqual(runner.calls, [])


class NetworkVerbTests(unittest.TestCase):
    HELPER = ['sudo', '-n', '/usr/libexec/pibuddycam/pibuddycam-priv']

    def test_network_apply_sends_the_request_on_stdin_only(self):
        runner = make_runner()
        body = '{"ssid": "Home", "psk": "%s"}' % PSK
        result = privileged.network_apply(body, runner=runner)
        self.assertTrue(result)
        self.assertEqual(runner.calls[0][0], self.HELPER + ['network-apply'])
        self.assertEqual(runner.calls[0][2], body)
        self.assertNotIn(PSK, ' '.join(all_argv(runner)))
        self.assertNotIn(PSK, result.reason)

    def test_network_apply_failure_reason_carries_no_secret(self):
        runner = make_runner(default=FakeResult(1, PSK, PSK))
        result = privileged.network_apply('{"psk": "%s"}' % PSK, runner=runner)
        self.assertFalse(result)
        self.assertNotIn(PSK, result.reason)

    def test_network_apply_requires_a_body(self):
        runner = make_runner()
        self.assertFalse(privileged.network_apply('', runner=runner))
        self.assertEqual(runner.calls, [])

    def test_hostname_apply_passes_the_label_as_argument(self):
        runner = make_runner()
        self.assertTrue(privileged.hostname_apply('cam-1', runner=runner))
        self.assertEqual(runner.calls[0][0], self.HELPER + ['hostname-apply', 'cam-1'])
        self.assertFalse(privileged.hostname_apply('', runner=runner))

    def test_timezone_apply_passes_the_zone_as_argument(self):
        runner = make_runner()
        self.assertTrue(privileged.timezone_apply('Europe/Berlin', runner=runner))
        self.assertEqual(runner.calls[0][0], self.HELPER + ['timezone-apply', 'Europe/Berlin'])
        self.assertFalse(privileged.timezone_apply('', runner=runner))
        self.assertFalse(privileged.timezone_apply(None, runner=runner))
        self.assertEqual(len(runner.calls), 1)

    def test_ntp_apply_verb(self):
        runner = make_runner()
        self.assertTrue(privileged.ntp_apply(runner=runner))
        self.assertEqual(runner.calls[0][0], self.HELPER + ['ntp-apply'])

    def test_wifi_scan_parses_stdout_and_reports_failure_as_none(self):
        out = 'HomeNet:81:WPA2\nOpen\\:Cafe:40:\nHomeNet:60:WPA2\n:70:WPA2\n'
        runner = make_runner(default=FakeResult(0, out))
        networks = privileged.wifi_scan(runner=runner)
        self.assertEqual(runner.calls[0][0], self.HELPER + ['wifi-scan'])
        self.assertEqual(networks, [
            {'ssid': 'HomeNet', 'signal': 81, 'secured': True},
            {'ssid': 'Open:Cafe', 'signal': 40, 'secured': False},
        ])
        self.assertIsNone(privileged.wifi_scan(runner=make_runner(default=FakeResult(2))))

    def test_old_image_unknown_verb_shows_exit_two_in_the_reason(self):
        runner = make_runner(default=FakeResult(2))
        result = privileged.network_apply('{}', runner=runner)
        self.assertIn('exit 2', result.reason)


class WrapperTests(unittest.TestCase):
    def test_start_camera_uses_sudo_no_prompt_helper(self):
        runner = make_runner()
        self.assertTrue(privileged.start_camera(runner=runner))
        self.assertEqual(
            runner.calls[0][0],
            ['sudo', '-n', '/usr/libexec/pibuddycam/pibuddycam-priv', 'start-camera'],
        )

    def test_stop_provisioning_and_hotspot_verbs(self):
        runner = make_runner()
        self.assertTrue(privileged.stop_provisioning(runner=runner))
        self.assertTrue(privileged.hotspot_start(runner=runner))
        self.assertTrue(privileged.hotspot_stop(runner=runner))
        verbs = [args[-1] for args, _t, _i in runner.calls]
        self.assertEqual(
            verbs, ['stop-provisioning', 'hotspot-start', 'hotspot-stop']
        )

    def test_install_update_uses_sudo_no_prompt_helper(self):
        runner = make_runner()
        result = privileged.install_update(runner=runner)
        self.assertTrue(result)
        self.assertEqual(
            runner.calls[0][0],
            ['sudo', '-n', '/usr/libexec/pibuddycam/pibuddycam-priv', 'install-update'],
        )

    def test_install_update_failure_is_bounded(self):
        runner = make_runner(default=FakeResult(1, ''))
        result = privileged.install_update(runner=runner)
        self.assertFalse(result)
        self.assertIn('install-update failed', result.reason)

    def test_check_update_uses_fixed_helper_verb_and_longer_timeout(self):
        runner = make_runner()
        result = privileged.check_update(runner=runner)
        self.assertTrue(result)
        self.assertEqual(
            runner.calls[0][0],
            ['sudo', '-n', '/usr/libexec/pibuddycam/pibuddycam-priv',
             'check-update'],
        )
        self.assertEqual(runner.calls[0][1], privileged.CHECK_TIMEOUT_SECONDS)

    def test_check_update_failure_is_bounded(self):
        runner = make_runner(default=FakeResult(1, ''))
        result = privileged.check_update(runner=runner)
        self.assertFalse(result)
        self.assertIn('check-update failed', result.reason)

    def test_reboot_uses_fixed_helper_verb(self):
        runner = make_runner()
        result = privileged.reboot(runner=runner)
        self.assertTrue(result)
        self.assertEqual(
            runner.calls[0][0],
            ['sudo', '-n', '/usr/libexec/pibuddycam/pibuddycam-priv', 'reboot'],
        )

    def test_reboot_failure_is_bounded(self):
        runner = make_runner(default=FakeResult(1, ''))
        result = privileged.reboot(runner=runner)
        self.assertFalse(result)
        self.assertIn('reboot failed', result.reason)

    def test_quality_restart_uses_fixed_helper_verb(self):
        runner = make_runner()
        result = privileged.quality_restart(runner=runner)
        self.assertTrue(result)
        self.assertEqual(
            runner.calls[0][0],
            ['sudo', '-n', '/usr/libexec/pibuddycam/pibuddycam-priv',
             'quality-restart'],
        )

    def test_camera_restart_uses_fixed_helper_verb(self):
        runner = make_runner()
        result = privileged.camera_restart(runner=runner)
        self.assertTrue(result)
        self.assertEqual(
            runner.calls[0][0],
            ['sudo', '-n', '/usr/libexec/pibuddycam/pibuddycam-priv',
             'camera-restart'],
        )

    def test_camera_restart_failure_is_falsy(self):
        # An older image whose helper lacks the verb exits 2: not an exception.
        runner = make_runner(default=FakeResult(2, ''))
        self.assertFalse(privileged.camera_restart(runner=runner))

    def test_start_camera_survives_the_helper_being_cut_off(self):
        # Starting the target stops the provisioning unit this code runs in, which
        # kills the sudo child (a signal exit). The start job was already queued,
        # so a target that is coming up is a success, not a failed hand-off.
        for state in ('activating\n', 'active\n'):
            runner = make_runner(handlers=[
                ('start-camera', FakeResult(-15, '')),
                ('is-active', FakeResult(3, state)),
            ])
            self.assertTrue(
                privileged.start_camera(runner=runner, sleep=lambda _s: None), state)

    def test_start_camera_still_fails_when_the_target_is_not_coming_up(self):
        for state in ('inactive\n', 'failed\n', ''):
            slept = []
            runner = make_runner(handlers=[
                ('start-camera', FakeResult(-15, '')),
                ('is-active', FakeResult(3, state)),
            ])
            self.assertFalse(privileged.start_camera(
                runner=runner, grace_polls=3, sleep=slept.append), state)
            # It re-checked for the grace period before giving up.
            probes = [c for c in runner.calls if 'is-active' in ' '.join(c[0])]
            self.assertEqual(len(probes), 3, state)
            self.assertEqual(slept, [1, 1], state)

    def test_start_camera_helper_failure_with_no_target_is_not_retried_forever(self):
        runner = make_runner(default=FakeResult(1, ''))
        self.assertFalse(privileged.start_camera(
            runner=runner, grace_polls=2, sleep=lambda _s: None))

    def test_start_camera_waits_for_active_after_a_clean_start(self):
        slept = []
        answers = iter([FakeResult(3, 'activating\n'), FakeResult(0, 'active\n')])

        def runner(args, timeout, input=None):
            if 'is-active' in args:
                return next(answers)
            return FakeResult(0, '')

        self.assertTrue(privileged.start_camera(runner=runner, sleep=slept.append))
        self.assertEqual(slept, [2])

    def test_start_camera_failure_returns_false(self):
        runner = make_runner(default=FakeResult(1, ''))
        self.assertFalse(privileged.start_camera(runner=runner, sleep=lambda _s: None))

    def test_activate_station_psk_is_stdin_only(self):
        runner = make_runner()
        result = privileged.activate_station('HomeNet', PSK, runner=runner)
        self.assertTrue(result)
        args = runner.calls[0][0]
        self.assertEqual(
            args[:4],
            ['sudo', '-n', '/usr/libexec/pibuddycam/pibuddycam-priv',
             'wifi-station-apply'],
        )
        self.assertEqual(args[4], 'HomeNet')
        self.assertNotIn(PSK, all_argv(runner))
        self.assertEqual(runner.calls[0][2], PSK)

    def test_activate_station_failure_reason_never_carries_psk(self):
        runner = make_runner(default=FakeResult(1, ''))
        result = privileged.activate_station('HomeNet', PSK, runner=runner)
        self.assertFalse(result)
        self.assertNotIn(PSK, result.reason)
        self.assertIn('wifi-station-apply failed', result.reason)

    def test_activate_station_validates_input_without_running(self):
        runner = make_runner()
        self.assertFalse(privileged.activate_station('', PSK, runner=runner))
        self.assertFalse(privileged.activate_station('HomeNet', 123, runner=runner))
        self.assertEqual(runner.calls, [])

    def test_timeout_is_reported(self):
        # The helper timed out and the target is not coming up either.
        runner = make_runner(
            timeout_match='start-camera',
            handlers=[('is-active', FakeResult(3, 'inactive\n'))],
        )
        result = privileged.start_camera(runner=runner, sleep=lambda _s: None)
        self.assertFalse(result)

    def test_missing_tool_is_reported(self):
        runner = make_runner(unavailable_match='start-camera')
        result = privileged._invoke('start-camera', runner=runner)
        self.assertFalse(result)
        self.assertIn('unavailable', result.reason)

    def test_generic_failure_does_not_leak(self):
        def runner(args, timeout, input=None):
            raise RuntimeError('argv=helper wifi-station-apply ' + PSK)

        result = privileged.activate_station('HomeNet', PSK, runner=runner)
        self.assertFalse(result)
        self.assertNotIn(PSK, result.reason)

    def test_privileged_result_is_falsy_on_failure(self):
        self.assertTrue(privileged.PrivilegedResult(True, ''))
        self.assertFalse(privileged.PrivilegedResult(False, 'x'))


class PrivilegedHotspotTests(unittest.TestCase):
    def test_start_and_stop_route_through_privileged(self):
        calls = []

        def fake_start(runner=None):
            calls.append('start')
            return privileged.PrivilegedResult(True, '')

        def fake_stop(runner=None):
            calls.append('stop')
            return privileged.PrivilegedResult(True, '')

        controller = privileged.PrivilegedHotspot()
        with patch.object(privileged, 'hotspot_start', side_effect=fake_start), \
                patch.object(privileged, 'hotspot_stop', side_effect=fake_stop):
            self.assertTrue(controller.start('PiBuddyCam-Setup-abc123'))
            self.assertTrue(controller.stop())
        self.assertEqual(calls, ['start', 'stop'])

    def test_status_and_is_active_delegate_to_hotspot(self):
        marker = hotspot.HotspotResult(True, '', active=True)
        with patch.object(privileged.hotspot, 'status', return_value=marker) as status, \
                patch.object(privileged.hotspot, 'is_active', return_value=marker) as is_active:
            controller = privileged.PrivilegedHotspot()
            self.assertIs(controller.status(), marker)
            self.assertIs(controller.is_active(), marker)
        status.assert_called_once()
        is_active.assert_called_once()


if __name__ == '__main__':
    unittest.main()
