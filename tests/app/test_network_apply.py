"""Root network transaction: argv, secrets, health wait, revert, hotspot."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import network_apply as na  # noqa: E402
import network_settings  # noqa: E402
import wifi_station  # noqa: E402

PSK = 'CANARY-WIFI-PSK-abcdef123456'

CONNECTED = (
    'GENERAL.STATE:100 (connected)\nIP4.ADDRESS[1]:192.0.2.10/24\nIP4.GATEWAY:192.0.2.1\n'
)


class R:
    def __init__(self, returncode=0, stdout=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ''


class FakeNm:
    """Scriptable nmcli/ping: ``links`` is consumed one entry per device show."""

    def __init__(self, links=(CONNECTED,), profile=None, add_rc=0, modify_rc=0,
                 up_rc=0, ping_rc=0):
        self.calls = []
        self.links = list(links)
        self.profile = profile          # stdout of ``connection show`` or None
        self.add_rc, self.modify_rc, self.up_rc, self.ping_rc = (
            add_rc, modify_rc, up_rc, ping_rc)
        self.passwd_contents = []

    def __call__(self, args, timeout, input=None):
        self.calls.append(list(args))
        joined = ' '.join(args)
        if 'passwd-file' in args:
            path = args[args.index('passwd-file') + 1]
            with open(path, encoding='utf-8') as handle:
                self.passwd_contents.append(handle.read())
            stat = os.stat(path).st_mode & 0o777
            self.passwd_mode = stat
        if 'connection show' in joined:
            if self.profile is None:
                return R(10)
            return R(0, self.profile)
        if 'device show' in joined:
            link = self.links.pop(0) if len(self.links) > 1 else self.links[0]
            return R(0, link)
        if args[:3] == ['nmcli', 'connection', 'add']:
            return R(self.add_rc)
        if args[:3] == ['nmcli', 'connection', 'modify']:
            return R(self.modify_rc)
        if args[:3] == ['nmcli', 'connection', 'up']:
            return R(self.up_rc)
        if args[0] == 'ping':
            return R(self.ping_rc)
        return R(0)

    def commands(self, verb):
        return [c for c in self.calls if c[:3] == ['nmcli', 'connection', verb]]


class Env(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.keyfile = str(root / 'pibuddycam-station.nmconnection')
        self.result = str(root / 'run' / 'network-result.json')
        self.time = [0.0]
        self.hotspot_calls = []

    def sleep(self, seconds):
        self.time[0] += seconds

    def run_tx(self, request, nm, hotspot_ok=True, **kw):
        def hotspot():
            self.hotspot_calls.append(1)
            return hotspot_ok
        checked = network_settings.validate_request(request)
        return na.run(
            checked, runner=nm, keyfile=self.keyfile, result_path=self.result,
            sleep=self.sleep, monotonic=lambda: self.time[0], clock=lambda: 42.0,
            hotspot_fn=hotspot, health_timeout=10, restore_timeout=6, **kw)

    def written(self):
        with open(self.result, encoding='utf-8') as handle:
            return json.load(handle)


class DhcpTests(Env):
    def test_dhcp_apply_succeeds(self):
        nm = FakeNm()
        state = self.run_tx({'ssid': 'Home', 'psk': PSK}, nm)
        self.assertEqual(state, 'applied')
        self.assertEqual(self.written()['state'], 'applied')
        add = nm.commands('add')[0]
        self.assertIn('ipv4.method', add)
        self.assertEqual(add[add.index('ipv4.method') + 1], 'auto')
        self.assertEqual(self.hotspot_calls, [])

    def test_psk_only_goes_through_the_passwd_file(self):
        nm = FakeNm()
        self.run_tx({'ssid': 'Home', 'psk': PSK}, nm)
        for call in nm.calls:
            self.assertNotIn(PSK, ' '.join(call))
        self.assertEqual(nm.passwd_contents, [f'802-11-wireless-security.psk:{PSK}\n'])
        self.assertEqual(nm.passwd_mode, 0o600)
        self.assertNotIn(PSK, json.dumps(self.written()))

    def test_blank_psk_on_the_same_ssid_keeps_the_stored_key(self):
        profile = '802-11-wireless.ssid:Home\n802-11-wireless-security.key-mgmt:wpa-psk\n'
        nm = FakeNm(profile=profile)
        state = self.run_tx({'ssid': 'Home', 'psk': ''}, nm)
        self.assertEqual(state, 'applied')
        self.assertEqual(nm.passwd_contents, [])                 # no credential file
        modify = nm.commands('modify')[0]
        self.assertIn('wifi-sec.key-mgmt', modify)               # security preserved
        self.assertNotIn('remove', [c[-2] for c in nm.commands('modify') if len(c) > 2])
        self.assertEqual(nm.commands('add'), [])                 # modified in place
        self.assertEqual(nm.commands('up')[-1], ['nmcli', 'connection', 'up',
                                                 wifi_station.CONNECTION_NAME])

    def test_blank_psk_with_a_different_ssid_is_an_open_network(self):
        profile = '802-11-wireless.ssid:Other\n802-11-wireless-security.key-mgmt:wpa-psk\n'
        nm = FakeNm(profile=profile)
        self.run_tx({'ssid': 'Home', 'psk': ''}, nm)
        removes = [c for c in nm.commands('modify') if 'remove' in c]
        self.assertTrue(removes)


class StaticTests(Env):
    REQ = {'ssid': 'Home', 'psk': PSK, 'ipv4_method': 'manual',
           'address': '192.0.2.10', 'prefix': 24, 'gateway': '192.0.2.1',
           'dns': ['192.0.2.1', '198.51.100.1']}

    def test_static_argv(self):
        nm = FakeNm()
        self.assertEqual(self.run_tx(self.REQ, nm), 'applied')
        add = nm.commands('add')[0]
        tail = add[add.index('--') + 1:]
        pairs = dict(zip(tail[::2], tail[1::2]))
        self.assertEqual(pairs['ipv4.method'], 'manual')
        self.assertEqual(pairs['ipv4.addresses'], '192.0.2.10/24')
        self.assertEqual(pairs['ipv4.gateway'], '192.0.2.1')
        self.assertEqual(pairs['ipv4.dns'], '192.0.2.1 198.51.100.1')
        self.assertEqual(pairs['ipv4.ignore-auto-dns'], 'yes')

    def test_switching_back_to_dhcp_clears_static_values(self):
        nm = FakeNm(profile='802-11-wireless.ssid:Home\n')
        self.run_tx({'ssid': 'Home', 'psk': PSK}, nm)
        modify = nm.commands('modify')[0]
        pairs = dict(zip(modify[4::2], modify[5::2]))
        self.assertEqual(pairs['ipv4.method'], 'auto')
        self.assertEqual(pairs['ipv4.addresses'], '')
        self.assertEqual(pairs['ipv4.gateway'], '')
        self.assertEqual(pairs['ipv4.ignore-auto-dns'], 'no')

    def test_unreachable_gateway_reverts(self):
        with open(self.keyfile, 'wb') as handle:
            handle.write(b'OLD PROFILE')
        nm = FakeNm(ping_rc=1)
        state = self.run_tx(self.REQ, nm)
        self.assertEqual(state, 'reverted')
        self.assertEqual(self.written()['reason'], 'gateway unreachable')
        with open(self.keyfile, 'rb') as handle:
            self.assertEqual(handle.read(), b'OLD PROFILE')
        self.assertEqual(oct(os.stat(self.keyfile).st_mode & 0o777), oct(0o600))

    def test_address_mismatch_is_not_healthy(self):
        other = CONNECTED.replace('192.0.2.10', '192.0.2.77')
        nm = FakeNm(links=(other,))
        with open(self.keyfile, 'wb') as handle:
            handle.write(b'OLD')
        self.assertEqual(self.run_tx(self.REQ, nm), 'reverted')


class RevertTests(Env):
    def test_activation_failure_restores_snapshot_and_reactivates(self):
        with open(self.keyfile, 'wb') as handle:
            handle.write(b'SNAPSHOT')
        nm = FakeNm(up_rc=4)
        # the reactivation "up" also returns 4, but the link (stubbed) is healthy
        state = self.run_tx({'ssid': 'Home', 'psk': PSK}, nm)
        self.assertEqual(state, 'reverted')
        self.assertIn(['nmcli', 'connection', 'reload'], nm.calls)
        self.assertEqual(nm.commands('up')[-1],
                         ['nmcli', 'connection', 'up', wifi_station.CONNECTION_NAME])
        with open(self.keyfile, 'rb') as handle:
            self.assertEqual(handle.read(), b'SNAPSHOT')
        self.assertEqual(self.hotspot_calls, [])
        self.assertNotIn(PSK, json.dumps(self.written()))

    def test_wrong_psk_reverts_and_the_old_profile_comes_back(self):
        with open(self.keyfile, 'wb') as handle:
            handle.write(b'SNAPSHOT')
        down = 'GENERAL.STATE:30 (disconnected)\n'
        nm = FakeNm()
        base = nm.__call__

        def scripted(args, timeout, input=None):
            joined = ' '.join(args)
            if args[:3] in (['nmcli', 'connection', 'add'],
                            ['nmcli', 'connection', 'modify']):
                with open(self.keyfile, 'wb') as handle:      # NM rewrites the keyfile
                    handle.write(b'NEW PROFILE')
            if 'device show' in joined:
                nm.calls.append(list(args))
                with open(self.keyfile, 'rb') as handle:
                    old_profile = handle.read() == b'SNAPSHOT'
                return R(0, CONNECTED if old_profile else down)
            return base(args, timeout, input)

        state = self.run_tx({'ssid': 'Home', 'psk': PSK}, scripted)
        self.assertEqual(state, 'reverted')
        with open(self.keyfile, 'rb') as handle:
            self.assertEqual(handle.read(), b'SNAPSHOT')
        self.assertEqual(self.hotspot_calls, [])

    def test_never_healthy_new_profile_with_dead_restore_starts_hotspot(self):
        with open(self.keyfile, 'wb') as handle:
            handle.write(b'SNAPSHOT')
        down = 'GENERAL.STATE:30 (disconnected)\n'
        nm = FakeNm(links=(down,))
        state = self.run_tx({'ssid': 'Home', 'psk': PSK}, nm)
        self.assertEqual(state, 'hotspot')
        self.assertEqual(self.hotspot_calls, [1])
        self.assertEqual(self.written()['state'], 'hotspot')

    def test_no_previous_profile_deletes_the_new_one_then_hotspot(self):
        down = 'GENERAL.STATE:30 (disconnected)\n'
        nm = FakeNm(links=(down,))
        state = self.run_tx({'ssid': 'Home', 'psk': PSK}, nm)
        self.assertEqual(state, 'hotspot')
        self.assertIn(['nmcli', 'connection', 'delete', wifi_station.CONNECTION_NAME],
                      nm.calls)

    def test_hotspot_failure_is_reported_as_reverted(self):
        down = 'GENERAL.STATE:30 (disconnected)\n'
        nm = FakeNm(links=(down,))
        state = self.run_tx({'ssid': 'Home', 'psk': PSK}, nm, hotspot_ok=False)
        self.assertEqual(state, 'reverted')
        self.assertIn('did not come back', self.written()['reason'])

    def test_result_is_secret_free_and_world_readable(self):
        nm = FakeNm(up_rc=4, links=('GENERAL.STATE:30 (disconnected)\n',))
        self.run_tx({'ssid': 'Home', 'psk': PSK}, nm)
        with open(self.result, encoding='utf-8') as handle:
            body = handle.read()
        self.assertNotIn(PSK, body)
        self.assertEqual(os.stat(self.result).st_mode & 0o777, 0o644)


class RequestFileTests(Env):
    def test_read_request_deletes_the_file_and_validates(self):
        path = os.path.join(self._tmp.name, 'req.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump({'ssid': 'Home', 'psk': PSK, 'junk': 1}, handle)
        request = na.read_request(path)
        self.assertEqual(request['ssid'], 'Home')
        self.assertNotIn('junk', request)
        self.assertFalse(os.path.exists(path))

    def test_invalid_request_is_rejected_but_still_deleted(self):
        path = os.path.join(self._tmp.name, 'req.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump({'ssid': '', 'psk': PSK}, handle)
        with self.assertRaises(network_settings.ValidationError):
            na.read_request(path)
        self.assertFalse(os.path.exists(path))

    def test_static_request_is_revalidated_as_root(self):
        path = os.path.join(self._tmp.name, 'req.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump({'ssid': 'Home', 'ipv4_method': 'manual', 'address': '192.0.2.10',
                       'prefix': 24, 'gateway': '203.0.113.1'}, handle)
        with self.assertRaises(network_settings.ValidationError):
            na.read_request(path)


class WifiStationCompatTests(unittest.TestCase):
    def test_default_commands_are_unchanged(self):
        add = wifi_station._add_command('Home', 'wlan0', True)
        self.assertEqual(add[-4:], ['ipv4.method', 'auto', 'wifi-sec.key-mgmt', 'wpa-psk'])
        modify = wifi_station._modify_command('Home', 'wlan0', False)
        self.assertEqual(modify[-2:], ['ipv4.method', 'auto'])


if __name__ == '__main__':
    unittest.main()
