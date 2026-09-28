"""The NetworkManager NTP dispatcher passes only validated server tokens."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "image" / "assets" / "networkmanager" / "dispatcher.d" / "50-pibuddycam-ntp"


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.app_root = Path(self._tmp.name)
        python = self.app_root / "venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        self.args_out = self.app_root / "args"
        python.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" > "' + str(self.args_out) + '"\n',
            encoding="utf-8",
        )
        python.chmod(0o755)

    def run_hook(self, iface="wlan0", action="up", servers=None):
        env = dict(os.environ, PIBUDDYCAM_APP_ROOT=str(self.app_root))
        env.pop("DHCP4_NTP_SERVERS", None)
        if servers is not None:
            env["DHCP4_NTP_SERVERS"] = servers
        result = subprocess.run(
            ["bash", str(HOOK), iface, action], env=env, capture_output=True, text=True)
        argv = self.args_out.read_text(encoding="utf-8").splitlines() \
            if self.args_out.exists() else None
        return result, argv

    def test_script_is_valid_bash_and_executable(self):
        self.assertTrue(os.access(HOOK, os.X_OK))
        self.assertEqual(subprocess.run(["bash", "-n", str(HOOK)]).returncode, 0)

    def test_passes_dhcp_servers_on_up_and_dhcp4_change(self):
        for action in ("up", "dhcp4-change"):
            self.args_out.unlink(missing_ok=True)
            result, argv = self.run_hook(action=action, servers="192.0.2.1 192.0.2.2")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(argv[1:], ["dhcp", "192.0.2.1", "192.0.2.2"])
            self.assertTrue(argv[0].endswith("ntp_apply.py"))

    def test_other_actions_and_loopback_do_nothing(self):
        for iface, action in (("wlan0", "down"), ("wlan0", "dhcp6-change"),
                              ("wlan0", "pre-up"), ("lo", "up"), ("", "up")):
            self.args_out.unlink(missing_ok=True)
            result, argv = self.run_hook(iface=iface, action=action, servers="192.0.2.1")
            self.assertEqual(result.returncode, 0)
            self.assertIsNone(argv, (iface, action))

    def test_no_servers_still_tells_ntp_apply_to_clear_dhcp_servers(self):
        _result, argv = self.run_hook(servers=None)
        self.assertEqual(argv[1:], ["dhcp"])

    def test_only_validated_tokens_are_passed(self):
        hostile = "192.0.2.1 ;reboot $(id) -rf ../x `id` a\\nb NTP=evil ok.example"
        _result, argv = self.run_hook(servers=hostile)
        self.assertEqual(argv[1:], ["dhcp", "192.0.2.1", "ok.example"])

    def test_leading_dash_and_punctuation_tokens_are_dropped(self):
        _result, argv = self.run_hook(servers="-x.example .example example. a_b ok")
        self.assertEqual(argv[1:], ["dhcp", "ok"])

    def test_at_most_three_servers(self):
        _result, argv = self.run_hook(servers="a b c d e")
        self.assertEqual(argv[1:], ["dhcp", "a", "b", "c"])

    def test_newlines_in_the_variable_split_tokens_not_lines(self):
        _result, argv = self.run_hook(servers="192.0.2.1\n192.0.2.2")
        self.assertEqual(argv[1:], ["dhcp", "192.0.2.1", "192.0.2.2"])


if __name__ == "__main__":
    unittest.main()
