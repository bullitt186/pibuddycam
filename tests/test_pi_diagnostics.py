"""WP-UI7 AC-17: bounded, redacted current-boot diagnostics.

Stdlib-only and hermetic: the ``journalctl``/``systemctl`` runner is a fake, so
no process, journal, or device is touched.
"""
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(PI_DIR))

import diagnostics  # noqa: E402


class FakeRunner:
    """Records fixed argv; returns configured stdout without a real process."""

    def __init__(self, stdout='', returncode=0, exc=None):
        self.stdout = stdout
        self.returncode = returncode
        self.exc = exc
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append((list(args), timeout))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(
            list(args), self.returncode, stdout=self.stdout, stderr='')


class CollectTests(unittest.TestCase):
    def test_uses_fixed_bounded_argv(self):
        runner = FakeRunner(stdout='boot line\n')
        diagnostics.collect_diagnostics(runner=runner, clock=lambda: 1.0)
        self.assertEqual(len(runner.calls), 2)
        journal_args, journal_timeout = runner.calls[1]
        self.assertEqual(journal_args[:5], [
            'journalctl', '-b', '--no-pager', '-o', 'short-iso'])
        self.assertIn(str(diagnostics.MAX_JOURNAL_LINES), journal_args)
        for unit in diagnostics.FIXED_UNITS:
            self.assertIn(unit, journal_args)
        self.assertEqual(journal_timeout, diagnostics.JOURNAL_TIMEOUT_SECONDS)
        # No unbounded/unsafe command is ever run.
        joined = ' '.join(journal_args)
        for forbidden in ('--since', '--until', 'environ', ' ps ', 'status'):
            self.assertNotIn(forbidden, joined)

    def test_failed_units_command_is_fixed(self):
        runner = FakeRunner()
        diagnostics.collect_diagnostics(runner=runner, clock=lambda: 1.0)
        failed_args, _timeout = runner.calls[0]
        self.assertEqual(failed_args, ['systemctl', '--failed', '--no-pager'])

    def test_output_is_byte_bounded_and_flagged(self):
        runner = FakeRunner(stdout='A' * (diagnostics.MAX_TEXT_BYTES * 2))
        document = diagnostics.collect_diagnostics(runner=runner, clock=lambda: 1.0)
        self.assertTrue(document['truncated'])
        self.assertLessEqual(document['bytes'], diagnostics.MAX_TEXT_BYTES + 64)

    def test_timeout_is_reported_without_raising(self):
        runner = FakeRunner(exc=subprocess.TimeoutExpired('journalctl', 1.0))
        document = diagnostics.collect_diagnostics(runner=runner, clock=lambda: 1.0)
        self.assertTrue(document['ok'])
        self.assertIn('unavailable', document['text'])

    def test_missing_tool_is_reported_without_raising(self):
        runner = FakeRunner(exc=FileNotFoundError(2, 'missing'))
        document = diagnostics.collect_diagnostics(runner=runner, clock=lambda: 1.0)
        self.assertIn('unavailable', document['text'])

    def test_nonzero_exit_is_reported(self):
        runner = FakeRunner(stdout='nope', returncode=1)
        document = diagnostics.collect_diagnostics(runner=runner, clock=lambda: 1.0)
        self.assertIn('unavailable', document['text'])

    def test_runner_exception_text_is_not_echoed(self):
        runner = FakeRunner(exc=RuntimeError('argv=/secret/path password=hunter2'))
        document = diagnostics.collect_diagnostics(runner=runner, clock=lambda: 1.0)
        self.assertNotIn('hunter2', document['text'])
        self.assertNotIn('/secret/path', document['text'])


class RedactionTests(unittest.TestCase):
    def _redact(self, text, secrets=()):
        return diagnostics.redact_diagnostics(text, secrets)

    def test_literal_secret_values(self):
        out = self._redact('token is abc123', ('abc123',))
        self.assertNotIn('abc123', out)
        self.assertIn('<redacted>', out)

    def test_authorization_and_cookie_headers(self):
        out = self._redact(
            'Authorization: Bearer abc.def\nCookie: session=xyz\nSet-Cookie: a=b')
        self.assertNotIn('abc.def', out)
        self.assertNotIn('session=xyz', out)
        self.assertNotIn('a=b', out)

    def test_password_token_psk_assignments(self):
        out = self._redact(
            'password=sup3rsecret\n'
            'mqtt_password: brokerpass\n'
            'token=prusatoken\n'
            'psk=HomeNetPass\n'
            'api_key=deadbeef\n')
        for canary in ('sup3rsecret', 'brokerpass', 'prusatoken',
                       'HomeNetPass', 'deadbeef'):
            self.assertNotIn(canary, out)

    def test_jwt_is_redacted(self):
        jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop'
        out = self._redact(f'credential {jwt}')
        self.assertNotIn(jwt, out)
        self.assertIn('<redacted-jwt>', out)

    def test_personal_paths_are_redacted(self):
        out = self._redact('opened /home/alice/private/config.toml and /root/.ssh/id')
        self.assertNotIn('/home/alice', out)
        self.assertNotIn('/root/.ssh', out)
        self.assertIn('<path>', out)

    def test_wifi_identifiers_are_redacted(self):
        out = self._redact('wifi-ssid=HomeNet wifi-psk=HomeNetPass')
        self.assertNotIn('HomeNet', out)
        self.assertNotIn('HomeNetPass', out)

    def test_networkmanager_style_ssid_and_psk_are_redacted(self):
        out = self._redact(
            "NetworkManager: SSID: HomeNet\n"
            "supplicant: ssid='HomeNet'\n"
            "wpa: psk=HomeNetPass\n")
        self.assertNotIn('HomeNet', out)
        self.assertNotIn('HomeNetPass', out)

    def test_log_output_never_carries_a_canary(self):
        runner = FakeRunner(exc=RuntimeError('token=leakme /home/bob'))

        with self.assertLogs('pibuddycam.diagnostics', level='WARNING') as captured:
            diagnostics.collect_diagnostics(runner=runner, clock=lambda: 1.0)
        joined = '\n'.join(captured.output)
        self.assertNotIn('leakme', joined)
        self.assertNotIn('/home/bob', joined)

    def test_command_line_and_environment_canaries(self):
        out = self._redact(
            'ExecStart=/usr/bin/python /opt/pibuddycam/main.py token=abc\n'
            'environ PATH=/usr/bin HOME=/home/bob password=xyz\n')
        self.assertNotIn('token=abc', out)
        self.assertNotIn('password=xyz', out)
        self.assertNotIn('/home/bob', out)


class ProviderTests(unittest.TestCase):
    def test_snapshot_before_refresh_is_collecting(self):
        provider = diagnostics.DiagnosticsProvider(runner=FakeRunner())
        snapshot = provider.snapshot()
        self.assertFalse(snapshot['available'])
        self.assertEqual(snapshot['state'], 'collecting')

    def test_refresh_is_synchronous_and_fresh(self):
        provider = diagnostics.DiagnosticsProvider(
            runner=FakeRunner(stdout='journal line\n'), clock=lambda: 1.0)
        document = provider.refresh()
        self.assertTrue(document['ok'])
        snapshot = provider.snapshot()
        self.assertTrue(snapshot['available'])
        self.assertTrue(snapshot['fresh'])
        self.assertIn('journal line', snapshot['text'])

    def test_call_starts_a_background_refresh(self):
        started = threading.Event()

        class BlockingRunner(FakeRunner):
            def __call__(self, args, timeout):
                started.set()
                return super().__call__(args, timeout)

        provider = diagnostics.DiagnosticsProvider(runner=BlockingRunner(stdout='x'))
        first = provider(secrets=())
        self.assertFalse(first['available'])
        self.assertTrue(started.wait(2.0))
        # Wait for the refresh to land.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if provider.snapshot()['available']:
                break
            time.sleep(0.01)
        self.assertTrue(provider.snapshot()['available'])

    def test_stop_is_idempotent_and_never_raises(self):
        provider = diagnostics.DiagnosticsProvider(runner=FakeRunner())
        provider.stop()
        provider.stop()

    def test_provider_redacts_with_supplied_secrets(self):
        provider = diagnostics.DiagnosticsProvider(
            runner=FakeRunner(stdout='token=abc123\n'), clock=lambda: 1.0)
        document = provider.refresh(secrets=('abc123',))
        self.assertNotIn('abc123', document['text'])


if __name__ == '__main__':
    unittest.main()
