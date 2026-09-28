"""NTP drop-in: precedence, content, removal, validation, restart-on-change."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import config_schema  # noqa: E402
import ntp_apply  # noqa: E402


class PrecedenceTests(unittest.TestCase):
    def test_custom_then_dhcp_then_nothing(self):
        self.assertEqual(ntp_apply.resolve_servers(['a.example'], ['192.0.2.1']), ['a.example'])
        self.assertEqual(ntp_apply.resolve_servers([], ['192.0.2.1']), ['192.0.2.1'])
        self.assertEqual(ntp_apply.resolve_servers([], []), [])
        self.assertEqual(ntp_apply.resolve_servers(None, None), [])

    def test_invalid_entries_are_dropped_never_written(self):
        servers = ntp_apply.resolve_servers(
            ['ok.example', 'bad host', 'x;y', '-lead', 'ok.example', '\nNTP=evil'], [])
        self.assertEqual(servers, ['ok.example'])

    def test_at_most_three(self):
        self.assertEqual(
            ntp_apply.resolve_servers([], ['a', 'b', 'c', 'd']), ['a', 'b', 'c'])

    def test_invalid_custom_falls_back_to_dhcp(self):
        self.assertEqual(ntp_apply.resolve_servers(['bad host'], ['192.0.2.1']), ['192.0.2.1'])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.dropin = str(root / 'run' / 'timesyncd.conf.d' / '50-pibuddycam.conf')
        self.dhcp = str(root / 'dhcp')
        self.device = str(root / 'device.toml')
        self.restarts = []

    def _configure(self, servers):
        cfg = config_schema.default_device()
        cfg['network']['ntp_servers'] = servers
        with open(self.device, 'w', encoding='utf-8') as handle:
            handle.write(config_schema.dumps_device(cfg))

    def _apply(self, **kw):
        return ntp_apply.apply(
            device_path=self.device, dropin_path=self.dropin, dhcp_path=self.dhcp,
            restart=lambda: self.restarts.append(1), **kw)

    def _content(self):
        with open(self.dropin, encoding='utf-8') as handle:
            return handle.read()

    def test_custom_servers_are_written(self):
        self._configure(['time.example', '192.0.2.5'])
        self.assertEqual(self._apply(), ['time.example', '192.0.2.5'])
        self.assertEqual(self._content(), '[Time]\nNTP=time.example 192.0.2.5\n')
        self.assertEqual(oct(os.stat(self.dropin).st_mode & 0o777), oct(0o644))
        self.assertEqual(self.restarts, [1])

    def test_dhcp_servers_are_used_when_nothing_is_configured(self):
        self._configure([])
        self.assertEqual(self._apply(dhcp=['192.0.2.1']), ['192.0.2.1'])
        self.assertEqual(self._content(), '[Time]\nNTP=192.0.2.1\n')

    def test_stored_dhcp_servers_survive_a_later_plain_apply(self):
        self._configure([])
        self._apply(dhcp=['192.0.2.1'])
        self._configure(['custom.example'])
        self._apply()
        self.assertIn('custom.example', self._content())
        self._configure([])                  # user clears the custom servers
        self.assertEqual(self._apply(), ['192.0.2.1'])

    def test_dropin_is_removed_when_empty(self):
        self._configure(['time.example'])
        self._apply()
        self._configure([])
        self.restarts.clear()
        self.assertEqual(self._apply(dhcp=[]), [])
        self.assertFalse(os.path.exists(self.dropin))
        self.assertEqual(self.restarts, [1])

    def test_no_restart_when_nothing_changed(self):
        self._configure(['time.example'])
        self._apply()
        self.restarts.clear()
        self._apply()
        self._apply(dhcp=['192.0.2.9'])      # custom still wins: same content
        self.assertEqual(self.restarts, [])

    def test_nothing_to_do_without_servers_does_not_restart(self):
        self._configure([])
        self._apply()
        self.assertEqual(self.restarts, [])

    def test_hostile_dhcp_tokens_are_dropped(self):
        self._configure([])
        self._apply(dhcp=['192.0.2.1', 'evil\nNTP=x', 'a b', '$(id)'])
        self.assertEqual(self._content(), '[Time]\nNTP=192.0.2.1\n')

    def test_corrupt_device_toml_means_no_custom_servers(self):
        with open(self.device, 'w', encoding='utf-8') as handle:
            handle.write('this is [not toml')
        self.assertEqual(self._apply(dhcp=['192.0.2.1']), ['192.0.2.1'])


class CliTests(unittest.TestCase):
    def test_usage_error(self):
        self.assertEqual(ntp_apply.main([]), 2)
        self.assertEqual(ntp_apply.main(['bogus']), 2)


if __name__ == '__main__':
    unittest.main()
