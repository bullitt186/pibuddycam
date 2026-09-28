"""Console-side network settings: validation, live status, apply control."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import config_schema  # noqa: E402
import network_settings as ns  # noqa: E402
import privileged  # noqa: E402

PSK = 'CANARY-WIFI-PSK-abcdef123456'


class FakeResult:
    def __init__(self, returncode=0, stdout=''):
        self.returncode = returncode
        self.stdout = stdout


def runner_for(mapping):
    def runner(args, timeout, input=None):
        key = ' '.join(args)
        for needle, out in mapping.items():
            if needle in key:
                return FakeResult(0, out)
        return FakeResult(1, '')
    return runner


class HostnameTests(unittest.TestCase):
    def test_valid_labels_are_lowercased(self):
        self.assertEqual(ns.validate_hostname('Print-Cam1'), 'print-cam1')
        self.assertEqual(ns.validate_hostname('a'), 'a')
        self.assertEqual(ns.validate_hostname('x' * 63), 'x' * 63)

    def test_invalid_labels_are_rejected(self):
        for bad in ('', ' ', '-a', 'a-', 'a_b', 'a.b', 'x' * 64, 'ü', 'a b', None, 5):
            with self.assertRaises(ns.ValidationError, msg=repr(bad)):
                ns.validate_hostname(bad)


class NtpTests(unittest.TestCase):
    def test_up_to_three_hosts_or_ipv4(self):
        self.assertEqual(
            ns.validate_ntp_servers(['pool.ntp.org', '192.0.2.1', 'time.example']),
            ['pool.ntp.org', '192.0.2.1', 'time.example'])
        self.assertEqual(ns.validate_ntp_servers([]), [])
        self.assertEqual(ns.validate_ntp_servers(None), [])

    def test_invalid_entries_are_rejected(self):
        for bad in (['a', 'b', 'c', 'd'], ['bad host'], ['-x.example'], ['x;y'],
                    [''], [1], 'pool.ntp.org', ['a' * 254]):
            with self.assertRaises(ns.ValidationError, msg=repr(bad)):
                ns.validate_ntp_servers(bad)


class RequestTests(unittest.TestCase):
    def test_dhcp_request(self):
        req = ns.validate_request({'ssid': ' Home ', 'psk': PSK})
        self.assertEqual(req, {'ssid': 'Home', 'psk': PSK, 'ipv4_method': 'auto'})

    def test_blank_password_is_allowed_and_kept_blank(self):
        self.assertEqual(ns.validate_request({'ssid': 'Home'})['psk'], '')

    def test_static_request(self):
        req = ns.validate_request({
            'ssid': 'Home', 'psk': '', 'ipv4_method': 'manual',
            'address': '192.0.2.10', 'prefix': 24, 'gateway': '192.0.2.1',
            'dns': ['192.0.2.1', '198.51.100.1', '192.0.2.1'],
        })
        self.assertEqual(req['dns'], ['192.0.2.1', '198.51.100.1'])
        self.assertEqual(req['prefix'], 24)

    def _static(self, **override):
        body = {
            'ssid': 'Home', 'ipv4_method': 'manual', 'address': '192.0.2.10',
            'prefix': 24, 'gateway': '192.0.2.1', 'dns': [],
        }
        body.update(override)
        return body

    def test_bad_ssid_and_psk(self):
        for body in (
            {}, {'ssid': ''}, {'ssid': 5}, {'ssid': 'x' * 33}, {'ssid': 'ü' * 17},
            {'ssid': 'a\nb'}, {'ssid': 'ok', 'psk': 'short'},
            {'ssid': 'ok', 'psk': 'x' * 64}, {'ssid': 'ok', 'psk': 5},
            {'ssid': 'ok', 'ipv4_method': 'dhcp'},
        ):
            with self.assertRaises(ns.ValidationError, msg=repr(body)):
                ns.validate_request(body)

    def test_ssid_limit_is_bytes_not_characters(self):
        ns.validate_request({'ssid': 'a' * 32})
        with self.assertRaises(ns.ValidationError):
            ns.validate_request({'ssid': 'ü' * 17})   # 34 bytes

    def test_static_field_rules(self):
        for override in (
            {'address': 'nope'}, {'address': None}, {'address': '::1'},
            {'prefix': 0}, {'prefix': 31}, {'prefix': 24.0}, {'prefix': True},
            {'prefix': '24'}, {'gateway': '198.51.100.1'},   # outside subnet
            {'gateway': '192.0.2.10'},                        # equals the address
            {'address': '192.0.2.0'}, {'address': '192.0.2.255'},
            {'dns': ['a.b']}, {'dns': ['192.0.2.1'] * 0 + [
                '192.0.2.1', '192.0.2.2', '192.0.2.3', '192.0.2.4']},
            {'dns': 'x'},
        ):
            with self.assertRaises(ns.ValidationError, msg=repr(override)):
                ns.validate_request(self._static(**override))

    def test_error_messages_never_contain_the_psk(self):
        try:
            ns.validate_request({'ssid': 'ok', 'psk': PSK[:5], 'ipv4_method': 'x'})
        except ns.ValidationError as e:
            self.assertNotIn(PSK[:5], str(e))


class StatusTests(unittest.TestCase):
    DEVICE = (
        'GENERAL.STATE:100 (connected)\n'
        'IP4.ADDRESS[1]:192.0.2.10/24\n'
        'IP4.GATEWAY:192.0.2.1\n'
        'IP4.DNS[1]:192.0.2.1\n'
        'IP4.DNS[2]:198.51.100.1\n'
    )
    PROFILE = (
        '802-11-wireless.ssid:Home\\:Net\n'
        'ipv4.method:manual\n'
        'ipv4.addresses:192.0.2.10/24\n'
        'ipv4.gateway:192.0.2.1\n'
        'ipv4.dns:192.0.2.1,198.51.100.1\n'
    )

    def test_link_status_static(self):
        link = ns.read_link_status(runner_for({
            'device show': self.DEVICE, 'connection show': self.PROFILE}))
        self.assertTrue(link['connected'])
        self.assertEqual(link['ssid'], 'Home:Net')
        self.assertEqual(link['address'], '192.0.2.10')
        self.assertEqual(link['prefix'], 24)
        self.assertEqual(link['gateway'], '192.0.2.1')
        self.assertEqual(link['dns'], ['192.0.2.1', '198.51.100.1'])
        self.assertEqual(link['ipv4']['method'], 'manual')
        self.assertEqual(link['ipv4']['dns'], ['192.0.2.1', '198.51.100.1'])

    def test_link_status_dhcp_has_no_static_fields(self):
        link = ns.read_link_status(runner_for({
            'device show': self.DEVICE,
            'connection show': '802-11-wireless.ssid:Home\nipv4.method:auto\n'}))
        self.assertEqual(link['ipv4'], {'method': 'auto'})

    def test_unreadable_nmcli_yields_disconnected(self):
        link = ns.read_link_status(runner_for({}))
        self.assertFalse(link['connected'])
        self.assertEqual(link['ssid'], '')

    def test_runner_exceptions_are_swallowed(self):
        def boom(args, timeout, input=None):
            raise OSError('gone')
        self.assertFalse(ns.read_link_status(boom)['connected'])
        self.assertFalse(ns.read_time_status(boom)['synchronized'])

    def test_time_status(self):
        status = ns.read_time_status(runner_for({
            'NTPSynchronized': 'yes\n', 'ServerName': 'time.example\n'}),
            now=lambda: 1234.9)
        self.assertEqual(status, {
            'synchronized': True, 'server': 'time.example', 'now': 1234})
        self.assertFalse(ns.read_time_status(
            runner_for({'NTPSynchronized': 'no\n'}))['synchronized'])


class ResultTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, 'result.json')

    def _write(self, doc):
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write(doc if isinstance(doc, str) else json.dumps(doc))

    def test_missing_or_garbage_is_none(self):
        self.assertIsNone(ns.read_result(self.path))
        self._write('not json')
        self.assertIsNone(ns.read_result(self.path))
        self._write({'state': 'exploded'})
        self.assertIsNone(ns.read_result(self.path))
        self._write([1])
        self.assertIsNone(ns.read_result(self.path))

    def test_reads_state_and_bounds_reason(self):
        self._write({'state': 'reverted', 'reason': 'x' * 500 + '\n', 'updated_at': 5})
        result = ns.read_result(self.path, now=lambda: 6)
        self.assertEqual(result['state'], 'reverted')
        self.assertEqual(len(result['reason']), 200)

    def test_ancient_applying_is_not_busy(self):
        self._write({'state': 'applying', 'updated_at': 0})
        result = ns.read_result(self.path, now=lambda: 10_000)
        self.assertEqual(result['state'], 'reverted')


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.device = str(root / 'device.toml')
        self.secrets = str(root / 'secrets.toml')
        self.result = str(root / 'result.json')
        self.clock = [1000.0]
        self.applied = []
        config_schema.save_pair(
            config_schema.default_device(), {'wifi': {'psk': 'oldoldold'}},
            self.device, self.secrets)

    def _controller(self, **kw):
        args = dict(
            runner=runner_for({}), apply_fn=self._apply, device_path=self.device,
            secrets_path=self.secrets, result_path=self.result,
            clock=lambda: self.clock[0], monotonic=lambda: self.clock[0])
        args.update(kw)
        return ns.NetworkController(**args)

    def _apply(self, body):
        self.applied.append(body)
        return privileged.PrivilegedResult(True)

    def _write_result(self, state, at):
        with open(self.result, 'w', encoding='utf-8') as handle:
            json.dump({'state': state, 'reason': '', 'updated_at': at}, handle)

    def test_apply_hands_json_to_the_helper_and_returns_202(self):
        ctl = self._controller()
        request = ns.validate_request({
            'ssid': 'Home', 'psk': PSK, 'ipv4_method': 'manual',
            'address': '192.0.2.10', 'prefix': 24, 'gateway': '192.0.2.1'})
        status, payload = ctl.apply(request)
        self.assertEqual(status, 202)
        self.assertEqual(payload['expected_address'], '192.0.2.10')
        self.assertEqual(json.loads(self.applied[0])['ssid'], 'Home')
        self.assertNotIn(PSK, json.dumps(payload))

    def test_second_apply_while_running_is_409(self):
        ctl = self._controller()
        self._write_result('applying', self.clock[0])
        self.assertEqual(ctl.apply({'ssid': 'Home', 'ipv4_method': 'auto'})[0], 409)
        self.assertEqual(self.applied, [])

    def test_pending_request_without_result_is_busy_until_result_or_timeout(self):
        ctl = self._controller()
        self.assertEqual(ctl.apply({'ssid': 'Home', 'ipv4_method': 'auto'})[0], 202)
        self.assertTrue(ctl.busy())
        self.assertEqual(ctl.apply({'ssid': 'Home', 'ipv4_method': 'auto'})[0], 409)
        self.clock[0] += 31
        self.assertFalse(ctl.busy())

    def test_an_old_result_does_not_finish_a_new_request(self):
        self._write_result('applied', 500.0)
        ctl = self._controller()
        self.assertEqual(ctl.apply({'ssid': 'Home', 'psk': PSK, 'ipv4_method': 'auto'})[0], 202)
        self.assertTrue(ctl.busy())
        ctl.status()
        wifi = config_schema.load_secrets(self.secrets)['wifi']
        self.assertEqual(wifi['psk'], 'oldoldold')

    def test_psk_is_persisted_only_after_applied(self):
        ctl = self._controller()
        ctl.apply({'ssid': 'Home', 'psk': PSK, 'ipv4_method': 'auto'})
        self.clock[0] += 5
        self._write_result('reverted', self.clock[0])
        ctl.status()
        self.assertEqual(config_schema.load_secrets(self.secrets)['wifi']['psk'], 'oldoldold')

        ctl.apply({'ssid': 'Home', 'psk': PSK, 'ipv4_method': 'auto'})
        self.clock[0] += 5
        self._write_result('applied', self.clock[0])
        status = ctl.status()
        self.assertEqual(config_schema.load_secrets(self.secrets)['wifi']['psk'], PSK)
        self.assertNotIn(PSK, json.dumps(status))
        self.assertTrue(status['psk_set'])

    def test_blank_psk_keeps_the_stored_secret(self):
        ctl = self._controller()
        ctl.apply({'ssid': 'Home', 'psk': '', 'ipv4_method': 'auto'})
        self.clock[0] += 5
        self._write_result('applied', self.clock[0])
        ctl.status()
        self.assertEqual(config_schema.load_secrets(self.secrets)['wifi']['psk'], 'oldoldold')

    def test_unknown_verb_maps_to_501_and_failure_to_502(self):
        old = self._controller(apply_fn=lambda body: privileged.PrivilegedResult(
            False, 'network-apply failed (exit 2)'))
        self.assertEqual(old.apply({'ssid': 'x', 'ipv4_method': 'auto'})[0], 501)
        broken = self._controller(apply_fn=lambda body: privileged.PrivilegedResult(
            False, 'network-apply failed (exit 1)'))
        self.assertEqual(broken.apply({'ssid': 'x', 'ipv4_method': 'auto'})[0], 502)
        none = self._controller(apply_fn=None)
        self.assertEqual(none.apply({'ssid': 'x', 'ipv4_method': 'auto'})[0], 501)

    def test_hostname_is_applied_then_persisted(self):
        calls = []
        ctl = self._controller(hostname_fn=lambda n: calls.append(n) or
                               privileged.PrivilegedResult(True))
        status, payload = ctl.set_hostname('cam-1')
        self.assertEqual((status, calls), (200, ['cam-1']))
        self.assertTrue(payload['reboot_recommended'])
        self.assertEqual(config_schema.load_device(self.device)['admin']['hostname'], 'cam-1')
        # the secrets file is untouched by the device-only change
        self.assertEqual(config_schema.load_secrets(self.secrets)['wifi']['psk'], 'oldoldold')

    def test_hostname_not_persisted_when_the_helper_fails(self):
        ctl = self._controller(hostname_fn=lambda n: privileged.PrivilegedResult(
            False, 'hostname-apply failed (exit 1)'))
        self.assertEqual(ctl.set_hostname('cam-1')[0], 502)
        self.assertEqual(config_schema.load_device(self.device)['admin']['hostname'], '')

    def test_ntp_servers_are_saved_and_applied(self):
        calls = []
        ctl = self._controller(ntp_fn=lambda: calls.append(1) or
                               privileged.PrivilegedResult(True))
        status, payload = ctl.set_ntp_servers(['time.example'])
        self.assertEqual(status, 200)
        self.assertTrue(payload['applied'])
        self.assertEqual(calls, [1])
        self.assertEqual(
            config_schema.load_device(self.device)['network']['ntp_servers'],
            ['time.example'])
        self.assertEqual(ctl.status()['ntp_servers'], ['time.example'])

    def test_scan_is_cached_and_rate_limited(self):
        import threading
        done = threading.Event()
        calls = []

        def scan():
            calls.append(1)
            done.set()
            return [{'ssid': 'Home', 'signal': 80, 'secured': True}]

        ctl = self._controller(scan_fn=scan)
        status, first = ctl.scan()
        self.assertEqual(status, 200)
        self.assertTrue(done.wait(2))
        for _ in range(100):
            if not ctl._scan_running:
                break
            threading.Event().wait(0.01)
        status, second = ctl.scan()
        self.assertEqual(second['networks'][0]['ssid'], 'Home')
        self.assertEqual(len(calls), 1)     # inside the minimum interval: cached
        self.clock[0] += ns.SCAN_MIN_INTERVAL_SECONDS + 1
        ctl.scan()
        self.assertTrue(done.wait(2))

    def test_scan_unavailable(self):
        self.assertEqual(self._controller(scan_fn=None).scan()[0], 501)


class ScanParserTests(unittest.TestCase):
    def test_dedup_sort_and_hidden(self):
        text = 'Weak:20:WPA2\nStrong:90:WPA2\nWeak:35:WPA2\n:70:WPA2\nOpen:50:\nDash:40:--\n'
        parsed = ns.parse_scan_output(text)
        self.assertEqual([n['ssid'] for n in parsed], ['Strong', 'Open', 'Dash', 'Weak'])
        self.assertEqual(parsed[-1]['signal'], 35)
        self.assertFalse(parsed[1]['secured'])
        self.assertFalse(parsed[2]['secured'])
        self.assertTrue(parsed[0]['secured'])

    def test_garbage_lines_are_ignored(self):
        self.assertEqual(ns.parse_scan_output('nonsense\n::\nA:x:WPA2\n'),
                         [{'ssid': 'A', 'signal': 0, 'secured': True}])
        self.assertEqual(ns.parse_scan_output(None), [])


if __name__ == '__main__':
    unittest.main()
