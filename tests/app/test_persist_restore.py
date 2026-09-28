"""GAP-PERSIST-01: persist_restore pure helpers + import safety."""
import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import persist_restore  # noqa: E402
import quality  # noqa: E402
import rotation  # noqa: E402


class FramesToPruneTests(unittest.TestCase):
    def test_within_budget_returns_empty(self):
        frames = [('a.jpg', 10, 1.0)]
        self.assertEqual(persist_restore.frames_to_prune(frames, 10, 100), [])
        self.assertEqual(persist_restore.frames_to_prune(frames, 10, 10), [])

    def test_selects_oldest_first(self):
        frames = [
            ('new.jpg', 100, 3.0),
            ('old.jpg', 100, 1.0),
            ('mid.jpg', 100, 2.0),
        ]
        self.assertEqual(
            persist_restore.frames_to_prune(frames, 300, 150),
            [('old.jpg', 100), ('mid.jpg', 100)],
        )

    def test_prunes_all_when_budget_exhausted(self):
        frames = [('b.jpg', 5, 2.0), ('a.jpg', 5, 1.0)]
        self.assertEqual(
            persist_restore.frames_to_prune(frames, 10, 0),
            [('a.jpg', 5), ('b.jpg', 5)],
        )

    def test_empty_frames(self):
        self.assertEqual(persist_restore.frames_to_prune([], 0, 0), [])


class QualityEnvValuesTests(unittest.TestCase):
    def test_tier_mapping(self):
        self.assertEqual(persist_restore.quality_env_values(1), (640, 480))
        self.assertEqual(persist_restore.quality_env_values(2), (1280, 720))
        self.assertEqual(persist_restore.quality_env_values(3), (1920, 1080))

    def test_unknown_tier_falls_back_to_default(self):
        self.assertEqual(
            persist_restore.quality_env_values(99),
            persist_restore.quality_env_values(quality.DEFAULT_QUALITY),
        )


class RestoreSettingsTests(unittest.TestCase):
    """state.json -> tmpfs env files, so settings survive a reboot."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        for target, name, value in (
            (quality, 'QUALITY_ENV', 'quality.env'),
            (rotation, 'ROTATION_ENV', 'rotation.env'),
        ):
            p = patch.object(target, name, os.path.join(self.dir, value))
            p.start()
            self.addCleanup(p.stop)
        for p in (patch.object(persist_restore, '_chown'),
                  patch.object(persist_restore.rtsp_control, 'write_mode',
                               return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def _restore(self, data):
        with patch.object(persist_restore.settings_store, 'load', return_value=data):
            persist_restore._restore_settings('svc')

    def test_rotation_is_materialized_next_to_quality(self):
        self._restore({'quality_tier': 2, 'rotation': 270})
        self.assertEqual(rotation.read_current(), 270)
        self.assertEqual(quality.read_current()[0], 2)
        persist_restore._chown.assert_any_call(rotation.ROTATION_ENV, 'svc')

    def test_invalid_or_missing_rotation_writes_nothing(self):
        for data in ({'quality_tier': 2}, {'rotation': 45}, {'rotation': '90'}):
            with self.subTest(data=data):
                self._restore(data)
                self.assertFalse(os.path.exists(rotation.ROTATION_ENV))


class PruneTimelapseTests(unittest.TestCase):
    """The free-space -> prune-limit conversion and the .avi/CSV safety."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.threshold = persist_restore.PRUNE_FREE_THRESHOLD_BYTES

    def tearDown(self):
        self._tmp.cleanup()

    def _file(self, name, size):
        with open(os.path.join(self.dir, name), 'wb') as f:
            f.write(b'x' * size)

    def test_prunes_oldest_frames_to_reach_threshold(self):
        for i in range(4):
            self._file(f'frame_{i}.jpg', 100)
        # 150 bytes below the threshold -> limit = 400 - 150 = 250 -> drop 2.
        with patch.object(
            persist_restore.shutil, 'disk_usage',
            return_value=SimpleNamespace(free=self.threshold - 150),
        ):
            persist_restore._prune_timelapse(self.dir, mount='/data')
        self.assertEqual(sorted(os.listdir(self.dir)), ['frame_2.jpg', 'frame_3.jpg'])

    def test_never_deletes_avi_or_csv(self):
        self._file('keep.avi', 100)
        self._file('.timelapse_videos.csv', 100)
        self._file('frame_0.jpg', 100)
        with patch.object(
            persist_restore.shutil, 'disk_usage',
            return_value=SimpleNamespace(free=0),
        ):
            persist_restore._prune_timelapse(self.dir, mount='/data')
        self.assertIn('keep.avi', os.listdir(self.dir))
        self.assertIn('.timelapse_videos.csv', os.listdir(self.dir))
        self.assertNotIn('frame_0.jpg', os.listdir(self.dir))

    def test_no_prune_when_above_threshold(self):
        self._file('frame_0.jpg', 100)
        with patch.object(
            persist_restore.shutil, 'disk_usage',
            return_value=SimpleNamespace(free=self.threshold + 1),
        ):
            persist_restore._prune_timelapse(self.dir, mount='/data')
        self.assertEqual(os.listdir(self.dir), ['frame_0.jpg'])


class BindMountTests(unittest.TestCase):
    """B4: the NetworkManager connection store bind mount."""

    def test_constants_point_at_the_durable_store(self):
        self.assertEqual(
            persist_restore.DATA_NETWORK_CONNECTIONS,
            '/data/network/system-connections',
        )
        self.assertEqual(
            persist_restore.NM_CONNECTIONS,
            '/etc/NetworkManager/system-connections',
        )

    def test_bind_mount_uses_mount_bind(self):
        with patch.object(persist_restore.os, 'makedirs'), \
                patch.object(persist_restore.os.path, 'ismount', return_value=False), \
                patch.object(
                    persist_restore.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, '', ''),
                ) as run:
            self.assertTrue(
                persist_restore._bind_mount('/src', '/dst')
            )
        self.assertEqual(run.call_args[0][0], ['mount', '--bind', '/src', '/dst'])

    def test_bind_mount_is_idempotent_when_already_mounted(self):
        with patch.object(persist_restore.os, 'makedirs'), \
                patch.object(persist_restore.os.path, 'ismount', return_value=True), \
                patch.object(persist_restore.subprocess, 'run') as run:
            self.assertTrue(persist_restore._bind_mount('/src', '/dst'))
        run.assert_not_called()

    def test_bind_mount_failure_is_isolated(self):
        with patch.object(persist_restore.os, 'makedirs'), \
                patch.object(persist_restore.os.path, 'ismount', return_value=False), \
                patch.object(
                    persist_restore.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 1, '', 'boom'),
                ):
            self.assertFalse(persist_restore._bind_mount('/src', '/dst'))

    def test_bind_mount_makedirs_failure_is_isolated(self):
        with patch.object(persist_restore.os, 'makedirs', side_effect=OSError('nope')), \
                patch.object(persist_restore.subprocess, 'run') as run:
            self.assertFalse(persist_restore._bind_mount('/src', '/dst'))
        run.assert_not_called()

    def test_main_binds_sdcard_and_nm_connections(self):
        calls = []
        with patch.object(persist_restore.settings_store, 'available', return_value=True), \
                patch.object(persist_restore, 'ensure_durable_layout'), \
                patch.object(persist_restore, '_provision_admin_tls'), \
                patch.object(persist_restore, '_apply_hostname'), \
                patch.object(persist_restore, '_restore_time_sync'), \
                patch.object(
                    persist_restore, '_bind_mount',
                    side_effect=lambda src, dst: calls.append((src, dst)) or True,
                ), \
                patch.object(persist_restore, '_restore_settings'), \
                patch.object(persist_restore, '_prune_timelapse'):
            self.assertEqual(persist_restore.main(), 0)
        self.assertEqual(
            calls,
            [
                (persist_restore.DATA_SDCARD, persist_restore.SD_MOUNT),
                (persist_restore.DATA_NETWORK_CONNECTIONS, persist_restore.NM_CONNECTIONS),
            ],
        )

    def test_main_orders_hostname_before_tls_and_time_sync_after_binds(self):
        order = []
        with patch.object(persist_restore.settings_store, 'available', return_value=True), \
                patch.object(persist_restore.migrations, 'run_pending', return_value=[]), \
                patch.object(persist_restore, 'ensure_durable_layout'), \
                patch.object(persist_restore, '_apply_hostname',
                             side_effect=lambda: order.append('hostname')), \
                patch.object(persist_restore, '_provision_admin_tls',
                             side_effect=lambda user: order.append('tls')), \
                patch.object(persist_restore, '_bind_mount',
                             side_effect=lambda s, d: order.append('bind') or True), \
                patch.object(persist_restore, '_restore_time_sync',
                             side_effect=lambda: order.append('time')), \
                patch.object(persist_restore, '_restore_settings'), \
                patch.object(persist_restore, '_prune_timelapse'):
            persist_restore.main()
        self.assertEqual(order, ['hostname', 'tls', 'bind', 'bind', 'time'])

    def test_time_sync_binds_the_durable_clock_and_restarts_timesyncd_once(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, '', '')

        with patch.object(persist_restore, '_bind_mount', return_value=True) as bind, \
                patch.object(persist_restore.os.path, 'realpath',
                             return_value='/var/lib/private/systemd/timesync'), \
                patch.object(persist_restore.ntp_apply, 'apply') as apply:
            self.assertTrue(persist_restore._restore_time_sync(runner=fake_run))
        bind.assert_called_once_with(
            persist_restore.DATA_TIMESYNC, '/var/lib/private/systemd/timesync')
        # The drop-in is written without its own restart; one restart follows.
        self.assertIsNotNone(apply.call_args.kwargs['restart'])
        self.assertEqual(calls, [['systemctl', 'try-restart', 'systemd-timesyncd']])

    def test_timesync_dir_is_durable_and_root_owned(self):
        self.assertEqual(persist_restore.DATA_TIMESYNC, '/data/pibuddycam/timesync')
        self.assertIn(persist_restore.DATA_TIMESYNC, persist_restore.ROOT_ONLY_DIRS)
        self.assertIn(
            (persist_restore.DATA_TIMESYNC, 0o755), persist_restore.DATA_LAYOUT)


class ApplyHostnameTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, 'device.toml')

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, hostname):
        import config_schema
        cfg = config_schema.default_device()
        cfg['admin']['hostname'] = hostname
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write(config_schema.dumps_device(cfg))

    def _apply(self, returncode=0):
        calls = []

        def runner(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, returncode, '', '')

        return persist_restore._apply_hostname(self.path, runner=runner), calls

    def test_applies_the_configured_hostname_as_transient(self):
        self._write('Print-Cam')
        ok, calls = self._apply()
        self.assertTrue(ok)
        self.assertEqual(calls, [['hostnamectl', 'set-hostname', '--transient', 'print-cam']])

    def test_unset_or_invalid_hostname_changes_nothing(self):
        for value in ('', 'bad name', '-x', 'a_b'):
            self._write(value)
            ok, calls = self._apply()
            self.assertFalse(ok, value)
            self.assertEqual(calls, [], value)

    def test_missing_config_changes_nothing(self):
        ok, calls = self._apply()
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_failing_hostnamectl_is_isolated(self):
        self._write('cam')
        ok, _calls = self._apply(returncode=1)
        self.assertFalse(ok)


class SessionPruneTests(unittest.TestCase):
    """Frames inside per-print session folders must not escape the guard."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.threshold = persist_restore.PRUNE_FREE_THRESHOLD_BYTES

    def tearDown(self):
        self._tmp.cleanup()

    def _file(self, rel, size, mtime):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(b'x' * size)
        os.utime(path, (mtime, mtime))

    def test_prunes_oldest_first_across_root_and_sessions(self):
        self._file('session_20260101-000000/timelapse_00-00-01-000.jpg', 100, 1000)
        self._file('timelapse_00-00-02-000.jpg', 100, 2000)
        self._file('session_20260102-000000/timelapse_00-00-03-000.jpg', 100, 3000)
        with patch.object(persist_restore.shutil, 'disk_usage',
                          return_value=SimpleNamespace(free=self.threshold - 200)):
            persist_restore._prune_timelapse(self.dir, mount='/data')
        self.assertFalse(os.path.exists(os.path.join(
            self.dir, 'session_20260101-000000', 'timelapse_00-00-01-000.jpg')))
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'timelapse_00-00-02-000.jpg')))
        self.assertTrue(os.path.exists(os.path.join(
            self.dir, 'session_20260102-000000', 'timelapse_00-00-03-000.jpg')))

    def test_emptied_session_folders_are_removed_but_videos_stay(self):
        self._file('session_20260101-000000/timelapse_00-00-01-000.jpg', 100, 1000)
        self._file('session_20260101-000000.avi', 100, 1000)
        with patch.object(persist_restore.shutil, 'disk_usage',
                          return_value=SimpleNamespace(free=0)):
            persist_restore._prune_timelapse(self.dir, mount='/data')
        self.assertEqual(os.listdir(self.dir), ['session_20260101-000000.avi'])

    def test_the_active_session_folder_is_kept_even_when_empty(self):
        import timelapse
        name = timelapse.open_session(self.dir, now=1_700_000_000)
        self._file('timelapse_00-00-01-000.jpg', 100, 1000)
        with patch.object(persist_restore.shutil, 'disk_usage',
                          return_value=SimpleNamespace(free=0)):
            persist_restore._prune_timelapse(self.dir, mount='/data')
        self.assertTrue(os.path.isdir(os.path.join(self.dir, name)))

    def test_symlinked_session_is_not_followed(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        with open(os.path.join(outside.name, 'timelapse_00-00-01-000.jpg'), 'wb') as f:
            f.write(b'x' * 100)
        os.symlink(outside.name, os.path.join(self.dir, 'session_20260101-000000'))
        with patch.object(persist_restore.shutil, 'disk_usage',
                          return_value=SimpleNamespace(free=0)):
            persist_restore._prune_timelapse(self.dir, mount='/data')
        self.assertTrue(os.path.exists(
            os.path.join(outside.name, 'timelapse_00-00-01-000.jpg')))


class ImportSafetyTests(unittest.TestCase):
    """Importing must not touch the filesystem (the module runs only via main)."""

    def test_no_module_level_io_calls(self):
        tree = ast.parse(Path(persist_restore.__file__).read_text())
        forbidden = {
            'makedirs', 'mkdir', 'remove', 'unlink', 'chown',
            'write_current', 'write_mode', 'save', 'run',
        }
        offenders = []
        for node in tree.body:
            # Function/class bodies only run when called; skip them.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                name = func.attr if isinstance(func, ast.Attribute) else (
                    func.id if isinstance(func, ast.Name) else ''
                )
                if name in forbidden:
                    offenders.append(name)
        self.assertEqual(offenders, [])

    def test_main_is_inert_when_data_unavailable(self):
        with patch.object(persist_restore.settings_store, 'available', return_value=False):
            self.assertEqual(persist_restore.main(), 0)


if __name__ == '__main__':
    unittest.main()
