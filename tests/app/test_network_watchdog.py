"""Network watchdog: thresholds, gating, hotspot start and recovery (fake nmcli/clock)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import network_settings  # noqa: E402
import network_watchdog as wd  # noqa: E402

UP = 'GENERAL.STATE:100 (connected)\nIP4.ADDRESS[1]:192.0.2.10/24\nIP4.GATEWAY:192.0.2.1\n'
DOWN = 'GENERAL.STATE:30 (disconnected)\n'


class R:
    def __init__(self, returncode=0, stdout=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ''


class Nm:
    """Fake nmcli: ``link`` is what ``device show`` returns; ``profile`` gates claimed."""

    def __init__(self, link=DOWN, profile=True):
        self.link = link
        self.profile = profile
        self.calls = []

    def __call__(self, args, timeout, input=None):
        self.calls.append(list(args))
        joined = ' '.join(args)
        if 'connection show' in joined:
            return R(0, '802-11-wireless.ssid:Home\n') if self.profile else R(10)
        if 'device show' in joined:
            return R(0, self.link)
        return R(0)


class Env(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = os.path.join(self._tmp.name, 'state.json')
        self.result = os.path.join(self._tmp.name, 'run', 'result.json')
        self.now = [10_000.0]
        self.started = []
        self.stopped = []
        self.hotspot_ok = True

    def start(self):
        self.started.append(self.now[0])
        return self.hotspot_ok

    def stop(self):
        self.stopped.append(self.now[0])
        return True

    def tick(self, nm, minutes=10, **kw):
        def sleep(seconds):
            self.now[0] += seconds
        return wd.tick(
            runner=nm, clock=lambda: self.now[0], minutes=minutes,
            state_path=self.state, result_path=self.result, start_hotspot=self.start,
            stop_hotspot=self.stop, sleep=sleep, monotonic=lambda: self.now[0], **kw)

    def advance(self, minutes):
        self.now[0] += minutes * 60

    def saved(self):
        with open(self.state, encoding='utf-8') as handle:
            return json.load(handle)

    def written(self):
        with open(self.result, encoding='utf-8') as handle:
            return json.load(handle)


class GateTests(Env):
    def test_disabled_does_nothing(self):
        nm = Nm()
        self.assertEqual(self.tick(nm, minutes=0), wd.DISABLED)
        self.assertEqual(nm.calls, [])

    def test_an_unclaimed_device_is_not_watched(self):
        nm = Nm(profile=False)
        self.assertEqual(self.tick(nm), wd.UNCLAIMED)
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.started, [])

    def test_a_running_console_change_is_never_disturbed(self):
        os.makedirs(os.path.dirname(self.result))
        with open(self.result, 'w', encoding='utf-8') as handle:
            json.dump({'state': 'applying', 'reason': '', 'updated_at': self.now[0]}, handle)
        self.assertEqual(self.tick(Nm()), wd.BUSY)
        self.assertEqual(self.started, [])

    def test_a_stale_applying_result_does_not_block_the_watchdog(self):
        os.makedirs(os.path.dirname(self.result))
        with open(self.result, 'w', encoding='utf-8') as handle:
            json.dump({'state': 'applying', 'reason': '', 'updated_at': self.now[0] - 3600}, handle)
        self.assertEqual(self.tick(Nm()), wd.WAITING)


class ThresholdTests(Env):
    def test_a_healthy_link_needs_no_state(self):
        self.assertEqual(self.tick(Nm(link=UP)), wd.HEALTHY)
        self.assertFalse(os.path.exists(self.state))

    def test_outage_is_counted_then_the_hotspot_starts_at_the_threshold(self):
        nm = Nm()
        self.assertEqual(self.tick(nm), wd.WAITING)
        self.assertEqual(self.saved()['down_since'], self.now[0])
        self.advance(9.5)
        self.assertEqual(self.tick(nm), wd.WAITING)
        self.assertEqual(self.started, [])
        self.advance(0.5)
        self.assertEqual(self.tick(nm), wd.HOTSPOT_STARTED)
        self.assertEqual(len(self.started), 1)
        self.assertTrue(self.saved()['hotspot'])
        self.assertEqual(self.written()['state'], 'hotspot')
        self.assertIn('10 minutes', self.written()['reason'])

    def test_a_recovering_link_resets_the_count(self):
        nm = Nm()
        self.tick(nm)
        self.advance(8)
        nm.link = UP
        self.assertEqual(self.tick(nm), wd.HEALTHY)
        self.assertFalse(os.path.exists(self.state) and self.saved())
        nm.link = DOWN
        self.advance(5)
        self.assertEqual(self.tick(nm), wd.WAITING)      # counting restarts from zero
        self.advance(6)
        self.assertEqual(self.tick(nm), wd.WAITING)
        self.assertEqual(self.started, [])

    def test_the_threshold_is_configurable(self):
        nm = Nm()
        self.tick(nm, minutes=2)
        self.advance(2)
        self.assertEqual(self.tick(nm, minutes=2), wd.HOTSPOT_STARTED)

    def test_a_failed_hotspot_start_is_retried_next_pass(self):
        nm = Nm()
        self.hotspot_ok = False
        self.tick(nm)
        self.advance(10)
        self.assertEqual(self.tick(nm), wd.WAITING)
        self.assertNotIn('hotspot', self.saved())
        self.hotspot_ok = True
        self.advance(1)
        self.assertEqual(self.tick(nm), wd.HOTSPOT_STARTED)

    def test_a_hotspot_start_that_raises_is_swallowed(self):
        def boom():
            raise RuntimeError('nmcli gone')
        nm = Nm()
        self.tick(nm)
        self.advance(10)
        outcome = wd.tick(
            runner=nm, clock=lambda: self.now[0], minutes=10, state_path=self.state,
            result_path=self.result, start_hotspot=boom, stop_hotspot=self.stop)
        self.assertEqual(outcome, wd.WAITING)

    def test_a_clock_that_jumped_back_restarts_the_count(self):
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        wd.save_state({'down_since': self.now[0] + 99999}, self.state)
        self.assertEqual(self.tick(Nm()), wd.WAITING)
        self.assertEqual(self.saved()['down_since'], self.now[0])


class HotspotPhaseTests(Env):
    def enter_hotspot(self, nm):
        self.tick(nm)
        self.advance(10)
        self.assertEqual(self.tick(nm), wd.HOTSPOT_STARTED)

    def test_the_hotspot_is_held_between_retries(self):
        nm = Nm()
        self.enter_hotspot(nm)
        self.advance(5)
        self.assertEqual(self.tick(nm), wd.HOTSPOT_HELD)
        self.assertEqual(len(self.started), 1)

    def test_the_station_is_retried_and_the_hotspot_stops_when_it_is_healthy(self):
        nm = Nm()
        self.enter_hotspot(nm)
        self.advance(10)
        nm.link = UP                      # the network is back
        self.assertEqual(self.tick(nm), wd.RECOVERED)
        self.assertIn(['nmcli', 'connection', 'up', 'pibuddycam-station'], nm.calls)
        self.assertEqual(len(self.stopped), 1)
        self.assertFalse(os.path.exists(self.state) and self.saved())
        self.assertEqual(self.written()['state'], 'applied')

    def test_a_failed_retry_restores_the_hotspot_and_waits_again(self):
        nm = Nm()
        self.enter_hotspot(nm)
        self.advance(10)
        starts = len(self.started)
        self.assertEqual(self.tick(nm), wd.RECOVERY_FAILED)
        self.assertEqual(len(self.started), starts + 1)
        self.assertEqual(self.stopped, [])
        self.advance(5)
        self.assertEqual(self.tick(nm), wd.HOTSPOT_HELD)

    def test_the_result_never_carries_a_secret_or_an_ssid(self):
        nm = Nm()
        self.enter_hotspot(nm)
        body = json.dumps(self.written())
        self.assertNotIn('Home', body)
        self.assertEqual(set(self.written()), {'state', 'reason', 'updated_at'})

    def test_the_console_reads_the_hotspot_result(self):
        self.enter_hotspot(Nm())
        result = network_settings.read_result(self.result, now=lambda: self.now[0])
        self.assertEqual(result['state'], 'hotspot')


class ConfigTests(unittest.TestCase):
    def test_threshold_from_the_environment(self):
        self.assertEqual(wd.threshold_minutes({}), wd.DEFAULT_MINUTES)
        self.assertEqual(wd.threshold_minutes({wd.ENV_MINUTES: '3'}), 3)
        self.assertEqual(wd.threshold_minutes({wd.ENV_MINUTES: '0'}), 0)
        for bad in ('x', '-1', '99999', ''):
            self.assertEqual(wd.threshold_minutes({wd.ENV_MINUTES: bad}), wd.DEFAULT_MINUTES, bad)

    def test_state_is_private_and_garbage_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 's.json')
            wd.save_state({'a': 1}, path)
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), oct(0o600))
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write('not json')
            self.assertEqual(wd.load_state(path), {})

    def test_cli_needs_the_tick_verb(self):
        self.assertEqual(wd.main([]), 2)
        self.assertEqual(wd.main(['bogus']), 2)


if __name__ == '__main__':
    unittest.main()
