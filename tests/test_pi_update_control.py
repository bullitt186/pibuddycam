"""WP-UI7 AC-15/AC-17: authenticated signed-update read/check/install control.

Stdlib-only and hermetic: every hardware action is a fake callable, no network,
no subprocess, and no real ``/data`` or updater config is read.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

PI_DIR = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(PI_DIR))

import update_control  # noqa: E402


class FakeAction:
    """A zero-argument action that records calls and returns a result."""

    def __init__(self, ok=True, reason='', delay=0.0):
        self.ok = ok
        self.reason = reason
        self.delay = delay
        self.calls = 0
        self.thread_names = []

    def __call__(self):
        self.calls += 1
        self.thread_names.append(threading.current_thread().name)
        if self.delay:
            time.sleep(self.delay)
        return SimpleNamespace(ok=self.ok, reason=self.reason)


def _manager(**overrides):
    check = overrides.pop('check_fn', FakeAction())
    install = overrides.pop('install_fn', FakeAction())
    defaults = dict(
        check_fn=check,
        install_fn=install,
        state_reader=lambda: None,
        last_check_reader=lambda: None,
        identity_fn=lambda: {},
        source_configured_fn=lambda: False,
        clock=lambda: 1000.0,
        monotonic=time.monotonic,
    )
    defaults.update(overrides)
    return update_control.UpdateManager(**defaults), check, install


class SourceConfiguredTests(unittest.TestCase):
    def _write(self, text):
        tmp = tempfile.NamedTemporaryFile('w', delete=False, suffix='.conf')
        tmp.write(text)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_missing_file_is_false(self):
        self.assertFalse(update_control.update_source_configured('/nonexistent'))

    def test_empty_and_commented_are_false(self):
        path = self._write('# PIBUDDYCAM_UPDATE_MANIFEST_URL=https://x\n')
        self.assertFalse(update_control.update_source_configured(path))

    def test_non_empty_is_true_without_returning_the_url(self):
        path = self._write('PIBUDDYCAM_UPDATE_MANIFEST_URL=https://updates.example/m.json\n')
        self.assertTrue(update_control.update_source_configured(path))

    def test_quoted_value_is_true(self):
        path = self._write('PIBUDDYCAM_UPDATE_MANIFEST_URL="https://updates.example/m.json"\n')
        self.assertTrue(update_control.update_source_configured(path))


class BuildUpdateViewTests(unittest.TestCase):
    def test_up_to_date(self):
        view = update_control.build_update_view(
            state={'installed_version': '1.2.3', 'latest_version': '1.2.3'},
            identity={'version': '1.2.3'}, now=1.0)
        self.assertEqual(view['state'], 'up-to-date')
        self.assertEqual(view['installed_version'], '1.2.3')

    def test_update_available(self):
        view = update_control.build_update_view(
            state={'latest_version': '1.2.4'},
            identity={'version': '1.2.3'}, now=1.0)
        self.assertEqual(view['state'], 'update-available')

    def test_installing_and_checking(self):
        installing = update_control.build_update_view(
            state={'in_progress': True}, identity={'version': '1.0'}, now=1.0)
        self.assertEqual(installing['state'], 'installing')
        checking = update_control.build_update_view(
            state={}, identity={'version': '1.0'}, now=1.0, checking=True)
        self.assertEqual(checking['state'], 'checking')

    def test_unknown_without_versions(self):
        view = update_control.build_update_view(state={}, identity={}, now=1.0)
        self.assertEqual(view['state'], 'unknown')

    def test_release_url_and_unknown_keys_are_dropped(self):
        view = update_control.build_update_view(
            state={
                'latest_version': '2.0.0',
                'release_url': 'https://updates.example/bundle.tar.zst',
                'bundle_url': 'https://updates.example/bundle.tar.zst',
                'channel': 'alpha',
                'signing_key': 'SECRET-KEY',
            },
            identity={'version': '1.0.0'}, now=1.0)
        blob = repr(view)
        self.assertNotIn('updates.example', blob)
        self.assertNotIn('SECRET-KEY', blob)
        self.assertNotIn('alpha', blob)
        self.assertNotIn('bundle_url', view)
        self.assertNotIn('release_url', view)

    def test_summary_and_reason_are_bounded(self):
        view = update_control.build_update_view(
            state={'latest_version': '2.0.0', 'release_summary': 'x' * 1000},
            identity={'version': '1.0.0'}, now=1.0)
        self.assertLessEqual(len(view['release_summary']), update_control.MAX_SUMMARY_CHARS)


class UpdateManagerReadTests(unittest.TestCase):
    def test_state_projects_readers(self):
        manager, _check, _install = _manager(
            state_reader=lambda: {'latest_version': '9.9.9', 'in_progress': False},
            last_check_reader=lambda: 1234.0,
            identity_fn=lambda: {'version': '1.0.0'},
            source_configured_fn=lambda: True,
        )
        view = manager.state()
        self.assertEqual(view['installed_version'], '1.0.0')
        self.assertEqual(view['latest_version'], '9.9.9')
        self.assertEqual(view['last_check'], 1234.0)
        self.assertTrue(view['source_configured'])
        self.assertFalse(view['checking'])
        self.assertFalse(view['installing'])

    def test_reader_exceptions_degrade(self):
        def boom():
            raise RuntimeError('boom')

        manager, _check, _install = _manager(
            state_reader=boom, last_check_reader=boom,
            identity_fn=boom, source_configured_fn=boom,
        )
        view = manager.state()
        self.assertEqual(view['state'], 'unknown')
        self.assertFalse(view['source_configured'])


class UpdateManagerActionTests(unittest.TestCase):
    def test_check_runs_on_a_daemon_thread(self):
        action = FakeAction()
        manager, _check, _install = _manager(check_fn=action)
        result = manager.check()
        self.assertTrue(result['ok'])
        manager.wait_for_idle()
        self.assertEqual(action.calls, 1)
        self.assertIn('update-check', action.thread_names)
        self.assertFalse(manager.state()['checking'])

    def test_duplicate_check_is_refused(self):
        action = FakeAction(delay=0.2)
        manager, _check, _install = _manager(check_fn=action)
        first = manager.check()
        self.assertTrue(first['ok'])
        second = manager.check()
        self.assertFalse(second['ok'])
        self.assertTrue(second['busy'])
        manager.wait_for_idle()

    def test_failed_check_records_a_bounded_reason(self):
        action = FakeAction(ok=False, reason='check ' + 'x' * 500)
        manager, _check, _install = _manager(check_fn=action)
        manager.check()
        manager.wait_for_idle()
        view = manager.state()
        self.assertIn('check_reason', view)
        self.assertLessEqual(len(view['check_reason']), update_control.MAX_REASON_CHARS)

    def test_check_exception_is_isolated(self):
        def boom():
            raise RuntimeError('boom')

        manager, _check, _install = _manager(check_fn=boom)
        manager.check()
        manager.wait_for_idle()
        self.assertEqual(manager.state()['check_reason'], 'update check failed')

    def test_install_runs_and_reports_failure(self):
        action = FakeAction(ok=False, reason='install failed')
        manager, _check, _install = _manager(install_fn=action)
        result = manager.install()
        self.assertTrue(result['ok'])
        manager.wait_for_idle()
        self.assertEqual(action.calls, 1)
        self.assertIn('update-install', action.thread_names)
        self.assertEqual(manager.state()['install_reason'], 'install failed')

    def test_duplicate_install_is_refused(self):
        action = FakeAction(delay=0.2)
        manager, _check, _install = _manager(install_fn=action)
        self.assertTrue(manager.install()['ok'])
        second = manager.install()
        self.assertFalse(second['ok'])
        self.assertTrue(second['busy'])
        manager.wait_for_idle()

    def test_install_is_refused_while_a_check_runs(self):
        check = FakeAction(delay=0.2)
        manager, _check, _install = _manager(check_fn=check)
        manager.check()
        blocked = manager.install()
        self.assertFalse(blocked['ok'])
        self.assertTrue(blocked['busy'])
        manager.wait_for_idle()

    def test_stale_check_flag_does_not_wedge_control(self):
        action = FakeAction()
        manager, _check, _install = _manager(
            check_fn=action, check_deadline=1.0)
        # Simulate a crashed worker: flag set, deadline long past.
        manager._checking = True
        manager._check_started = 0.0
        manager._monotonic = lambda: 1_000_000.0
        result = manager.check()
        self.assertTrue(result['ok'])
        manager.wait_for_idle()

    def test_actions_take_no_arguments(self):
        import inspect
        manager, _check, _install = _manager()
        self.assertEqual(
            list(inspect.signature(manager.check).parameters), [])
        self.assertEqual(
            list(inspect.signature(manager.install).parameters), [])

    def test_close_refuses_new_actions(self):
        manager, _check, _install = _manager()
        manager.close()
        self.assertFalse(manager.check()['ok'])
        self.assertFalse(manager.install()['ok'])

    def test_close_joins_a_finished_worker(self):
        action = FakeAction()
        manager, _check, _install = _manager(check_fn=action)
        manager.check()
        manager.close()
        self.assertEqual(action.calls, 1)

    def test_fixed_action_callables_are_used_verbatim(self):
        calls = []
        manager, _check, _install = _manager(
            check_fn=lambda: calls.append('check') or SimpleNamespace(ok=True),
            install_fn=lambda: calls.append('install') or SimpleNamespace(ok=True),
        )
        manager.check()
        manager.wait_for_idle()
        manager.install()
        manager.wait_for_idle()
        self.assertEqual(calls, ['check', 'install'])


if __name__ == '__main__':
    unittest.main()
