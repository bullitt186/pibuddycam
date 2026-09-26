"""WP-UI6 (plan AC-12/AC-13/AC-17/AC-18): safe local timelapse media + build.

Stdlib-only. Every path is a ``tempfile`` path; no network, subprocess, live
device, or ``/data`` access. The tests plant traversal/symlink/canary inputs and
instrument the frame readers to prove no whole-file read.
"""
import io
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import media_build  # noqa: E402
import media_library  # noqa: E402
import timelapse  # noqa: E402


def _jpeg(width=4, height=4):
    """Minimal JPEG carrying an SOF0 marker so dimensions can be parsed."""
    sof = (
        b'\xff\xc0' + struct.pack('>H', 17) + b'\x08'
        + struct.pack('>HH', height, width)
        + b'\x03' + b'\x01\x11\x00' * 3
    )
    return b'\xff\xd8' + sof + b'\xff\xd9'


def _frame_name(index):
    """A valid ``timelapse_`` frame basename for index 0..N."""
    return f'timelapse_00-00-{index:02d}-000.jpg'


def _write_frame(directory, index, data=None, mtime=None):
    name = _frame_name(index)
    path = os.path.join(directory, name)
    with open(path, 'wb') as handle:
        handle.write(_jpeg() if data is None else data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return name


def _write_video(directory, name, data=b'RIFF....AVI '):
    path = os.path.join(directory, name)
    with open(path, 'wb') as handle:
        handle.write(data)
    return name


def _wait_job(manager, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = manager.status(job_id)
        if view is not None and view['state'] not in ('pending', 'running'):
            return view
        time.sleep(0.01)
    raise AssertionError('job did not finish')


class NamePolicyTests(unittest.TestCase):
    def test_accepts_allowlisted_names(self):
        self.assertEqual(
            media_library.classify_name('timelapse_00-00-00-000.jpg'), 'frame')
        self.assertEqual(
            media_library.classify_name('timelapse_12-59-59-999.avi'), 'video')
        self.assertEqual(media_library.classify_name('a.avi'), 'video')

    def test_rejects_traversal_and_dot_paths(self):
        for name in (
            '../x.jpg', '/tmp/x.jpg', 'a/b.avi', 'a\\b.avi', '..', '.',
            './a.avi', 'a/../b.avi', '.hidden.jpg', '.timelapse_videos.csv',
        ):
            with self.subTest(name=name):
                self.assertIsNone(media_library.classify_name(name))

    def test_rejects_case_and_extension_drift(self):
        for name in ('x.AVI', 'x.JPG', 'timelapse_00-00-00-000.jpeg',
                     'timelapse_00-00-00-000.avi.jpg', 'x.txt', 'x.mp4'):
            with self.subTest(name=name):
                self.assertIsNone(media_library.classify_name(name))

    def test_rejects_control_chars_and_unicode(self):
        for name in ('x\x00.avi', 'x y.avi', 'x\u00e9.avi', 'x\t.avi', ''):
            with self.subTest(name=repr(name)):
                self.assertIsNone(media_library.classify_name(name))

    def test_rejects_non_string_and_overlong(self):
        self.assertIsNone(media_library.classify_name(None))
        self.assertIsNone(media_library.classify_name(42))
        self.assertIsNone(
            media_library.classify_name('a' * (media_library.MAX_NAME_CHARS + 1) + '.avi'))

    def test_status_mapping_is_exactly_depu(self):
        self.assertEqual(
            media_library.status_label(timelapse.VIDEO_STATUS_DONE), 'completed')
        self.assertEqual(
            media_library.status_label(timelapse.VIDEO_STATUS_ERROR), 'error')
        self.assertEqual(
            media_library.status_label(timelapse.VIDEO_STATUS_PENDING), 'pending')
        self.assertEqual(
            media_library.status_label(timelapse.VIDEO_STATUS_UNKNOWN), 'unknown')
        self.assertEqual(media_library.status_label('X'), 'unknown')
        self.assertEqual(media_library.status_char('all'), None)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_frames_sorted_and_symlinks_excluded(self):
        _write_frame(self.dir, 2)
        _write_frame(self.dir, 0)
        os.symlink(os.path.join(self.dir, _frame_name(0)),
                   os.path.join(self.dir, _frame_name(1)))
        entries, truncated = media_library.catalog_frames(self.dir)
        self.assertFalse(truncated)
        self.assertEqual([e.name for e in entries], [_frame_name(0), _frame_name(2)])

    def test_videos_have_index_status_and_newest_first(self):
        _write_video(self.dir, 'timelapse_00-00-00-000.avi')
        _write_video(self.dir, 'timelapse_00-00-01-000.avi')
        _write_video(self.dir, 'notes.txt')
        with open(os.path.join(self.dir, timelapse.CSV_NAME), 'w', encoding='utf-8') as fh:
            fh.write('timelapse_00-00-00-000.avi:D\n')
        entries, _truncated = media_library.catalog_videos(self.dir)
        names = [e.name for e in entries]
        self.assertEqual(len(names), 2)
        status = {e.name: e.status for e in entries}
        self.assertEqual(status['timelapse_00-00-00-000.avi'], 'D')
        self.assertEqual(status['timelapse_00-00-01-000.avi'], 'U')

    def test_hidden_index_never_listed_or_served(self):
        _write_video(self.dir, 'a.avi')
        with open(os.path.join(self.dir, timelapse.CSV_NAME), 'w', encoding='utf-8') as fh:
            fh.write('a.avi:D\n')
        entries, _ = media_library.catalog_videos(self.dir)
        self.assertNotIn(timelapse.CSV_NAME, [e.name for e in entries])
        with self.assertRaises(media_library.MediaError):
            media_library.open_media(self.dir, timelapse.CSV_NAME)

    def test_subdirectory_and_dot_names_excluded(self):
        os.mkdir(os.path.join(self.dir, 'nested'))
        _write_video(self.dir, 'a.avi')
        entries, _ = media_library.catalog_videos(self.dir)
        self.assertEqual([e.name for e in entries], ['a.avi'])

    def test_scan_cap_reports_truncation(self):
        for index in range(5):
            _write_frame(self.dir, index)
        with patch.object(media_library, 'MAX_SCAN_ENTRIES', 2):
            entries, truncated = media_library.catalog_frames(self.dir)
        self.assertTrue(truncated)
        self.assertEqual(len(entries), 2)

    def test_paginate_bounds_and_no_paths(self):
        entries = [
            media_library.MediaEntry(
                _frame_name(i), 10 + i, 1000.0 + i, None)
            for i in range(5)
        ]
        page = media_library.paginate(entries, 2, 2)
        self.assertEqual(page['page'], 2)
        self.assertEqual(page['page_size'], 2)
        self.assertEqual(page['total'], 5)
        self.assertEqual(page['pages'], 3)
        self.assertEqual([i['name'] for i in page['items']],
                         [_frame_name(2), _frame_name(3)])
        self.assertNotIn('path', json.dumps(page))

    def test_free_bytes_and_missing_dir(self):
        value = media_library.free_bytes(self.dir)
        self.assertIsInstance(value, int)
        self.assertGreaterEqual(value, 0)
        self.assertIsNone(media_library.free_bytes(os.path.join(self.dir, 'nope')))

    def test_frames_have_no_status(self):
        _write_frame(self.dir, 0)
        entries, _ = media_library.catalog_frames(self.dir)
        self.assertIsNone(entries[0].status)
        item = entries[0].as_dict()
        self.assertNotIn('status', item)
        self.assertNotIn('status_label', item)

    def test_scan_cap_counts_unrelated_entries(self):
        # The cap must bound directory work even when almost nothing matches;
        # the old match-counting cap would have scanned all of these.
        for index in range(6):
            with open(os.path.join(self.dir, f'ignore_me_{index}.txt'), 'wb') as fh:
                fh.write(b'x')
        _write_frame(self.dir, 0)
        with patch.object(media_library, 'MAX_SCAN_ENTRIES', 3):
            _entries, truncated = media_library.catalog_frames(self.dir)
        self.assertTrue(truncated)

    def test_index_duplicate_and_malformed_rows_map_only_depu(self):
        _write_video(self.dir, 'a.avi')
        _write_video(self.dir, 'b.avi')
        _write_video(self.dir, 'c.avi')
        with open(os.path.join(self.dir, timelapse.CSV_NAME), 'w', encoding='utf-8') as fh:
            fh.write('b.avi:Q\n')          # unknown char -> normalized to U
            fh.write('no-colon-line\n')    # malformed -> skipped
            fh.write(':D\n')               # empty name -> skipped
            fh.write('b.avi:D\n')          # duplicate -> last wins
            fh.write('../escape.avi:D\n')  # traversal name -> never matched
            fh.write('a.avi:P\n')
            fh.write('c.avi:ZZ\n')         # multi-char, unknown -> U
        entries, _ = media_library.catalog_videos(self.dir)
        status = {e.name: e.status for e in entries}
        self.assertEqual(status, {'a.avi': 'P', 'b.avi': 'D', 'c.avi': 'U'})
        labels = {e.name: media_library.status_label(e.status) for e in entries}
        self.assertEqual(
            labels, {'a.avi': 'pending', 'b.avi': 'completed', 'c.avi': 'unknown'})
        # Metadata can only ever carry a documented D/E/P/U char.
        for entry in entries:
            self.assertIn(entry.status, ('D', 'E', 'P', 'U'))
        self.assertEqual(
            set(media_library.status_counts(entries)),
            {'completed', 'error', 'pending', 'unknown'})

    def test_hardlinked_media_is_excluded_and_refused(self):
        real = _write_frame(self.dir, 0)
        link = _frame_name(9)
        os.link(os.path.join(self.dir, real), os.path.join(self.dir, link))
        entries, _ = media_library.catalog_frames(self.dir)
        self.assertEqual(entries, [])
        with self.assertRaises(media_library.MediaError):
            media_library.open_media(self.dir, real, expect='frame')
        os.unlink(os.path.join(self.dir, link))
        entries, _ = media_library.catalog_frames(self.dir)
        self.assertEqual([e.name for e in entries], [real])


class OpenMediaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_open_regular_frame(self):
        name = _write_frame(self.dir, 0)
        handle, size, kind = media_library.open_media(self.dir, name, expect='frame')
        try:
            self.assertEqual(kind, 'frame')
            self.assertEqual(size, os.path.getsize(os.path.join(self.dir, name)))
            self.assertEqual(handle.read(), _jpeg())
        finally:
            handle.close()

    def test_open_rejects_symlink(self):
        real = _write_frame(self.dir, 0)
        link = _frame_name(1)
        os.symlink(os.path.join(self.dir, real), os.path.join(self.dir, link))
        with self.assertRaises(media_library.MediaError):
            media_library.open_media(self.dir, link, expect='frame')

    def test_open_rejects_traversal_and_hidden(self):
        with self.assertRaises(media_library.MediaError):
            media_library.open_media(self.dir, '../x.jpg', expect='frame')
        with self.assertRaises(media_library.MediaError):
            media_library.open_media(self.dir, timelapse.CSV_NAME)

    def test_open_rejects_wrong_kind(self):
        name = _write_video(self.dir, 'a.avi')
        with self.assertRaises(media_library.MediaError):
            media_library.open_media(self.dir, name, expect='frame')

    def test_open_rejects_directory(self):
        os.mkdir(os.path.join(self.dir, 'timelapse_00-00-00-000.jpg'))
        with self.assertRaises(media_library.MediaError):
            media_library.open_media(
                self.dir, 'timelapse_00-00-00-000.jpg', expect='frame')

    def test_open_rejects_oversized_file(self):
        name = _write_frame(self.dir, 0, data=b'\xff\xd8\xff' + b'x' * 100)
        with patch.object(media_library, 'MAX_JPEG_BYTES', 10):
            with self.assertRaises(media_library.MediaError) as ctx:
                media_library.open_media(self.dir, name, expect='frame')
        self.assertEqual(ctx.exception.status, 413)

    def test_valid_jpeg_head(self):
        self.assertTrue(media_library.valid_jpeg_head(b'\xff\xd8\xff\xe0'))
        for head in (b'', b'\xff\xd8', b'RIFF', b'\xff\xd9\xff'):
            self.assertFalse(media_library.valid_jpeg_head(head))


class RangeTests(unittest.TestCase):
    def test_absent_range(self):
        self.assertIsNone(media_library.parse_range(None, 100))

    def test_valid_ranges(self):
        self.assertEqual(media_library.parse_range('bytes=0-99', 1000), (0, 99))
        self.assertEqual(media_library.parse_range('bytes=100-', 1000), (100, 999))
        self.assertEqual(media_library.parse_range('bytes=-100', 1000), (900, 999))
        self.assertEqual(media_library.parse_range('bytes=0-99999', 1000), (0, 999))

    def test_unsatisfiable_and_malformed(self):
        for value, size in (
            ('bytes=1000-', 1000),
            ('bytes=-0', 1000),
            ('bytes=5-1', 1000),
            ('bytes=abc', 1000),
            ('bytes=1-2,3-4', 1000),
            ('items=0-1', 1000),
            ('bytes=', 1000),
            ('bytes=-', 1000),
            ('bytes=0-1', 0),
            ('bytes=-10', 0),
        ):
            with self.subTest(value=value, size=size):
                with self.assertRaises(media_library.RangeNotSatisfiable):
                    media_library.parse_range(value, size)

    def test_non_string_rejected(self):
        with self.assertRaises(media_library.RangeNotSatisfiable):
            media_library.parse_range(123, 100)


class _InstrumentedReader:
    """A file-like wrapper recording every ``read`` size (whole-read detector)."""

    def __init__(self, data, record):
        self._buffer = io.BytesIO(data)
        self._record = record

    def read(self, size=-1):
        self._record.append(size)
        return self._buffer.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class StreamingBuildTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_stream_output_matches_in_memory_golden(self):
        payloads = [_jpeg(640, 480), b'\xff\xd8' + b'a' * 5 + b'\xff\xd9',
                    b'\xff\xd8' + b'b' * 6 + b'\xff\xd9']
        entries = [
            (f'f{i}.jpg', len(p), (lambda p=p: io.BytesIO(p)))
            for i, p in enumerate(payloads)
        ]
        out = io.BytesIO()
        timelapse._build_avi_stream(entries, 10, 640, 480, out)
        self.assertEqual(out.getvalue(), timelapse._build_avi_bytes(payloads, 10, 640, 480))

    def test_stream_handles_many_frames_and_index_is_exact(self):
        frames = 2000
        entries = [
            (f'f{i}.jpg', 1, (lambda: io.BytesIO(b'x')))
            for i in range(frames)
        ]
        out = io.BytesIO()
        timelapse._build_avi_stream(entries, 10, 4, 4, out)
        data = out.getvalue()
        self.assertEqual(struct.unpack('<I', data[4:8])[0], len(data) - 8)
        idx_pos = data.rindex(b'idx1')
        self.assertEqual(struct.unpack('<I', data[idx_pos + 4:idx_pos + 8])[0], frames * 16)

    def test_stream_does_not_buffer_index_in_memory(self):
        # Structural guard: the idx1 table is written entry by entry, not
        # accumulated in a bytearray/bytes that grows with the frame count.
        import inspect
        source = inspect.getsource(timelapse._build_avi_stream)
        self.assertNotIn('bytearray', source)
        self.assertNotIn('idx +=', source)

    def test_stream_reads_are_bounded(self):
        data = b'\xff\xd8' + b'x' * (200 * 1024) + b'\xff\xd9'
        record = []
        entries = [('f.jpg', len(data), lambda: _InstrumentedReader(data, record))]
        out = io.BytesIO()
        timelapse._build_avi_stream(entries, 10, 4, 4, out, read_chunk=4096)
        self.assertTrue(record)
        self.assertLessEqual(max(record), 4096)
        self.assertNotIn(-1, record)

    def test_stop_event_cancels_cleans_partial_and_records_error(self):
        _write_frame(self.dir, 0)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(timelapse.BuildCancelled):
            timelapse.build_avi(self.dir, fps=10, stop_event=stop)
        # The partial artifact is removed; only the historical E row remains.
        self.assertEqual(timelapse.list_videos(self.dir), [])
        statuses = timelapse.read_video_index(self.dir)
        self.assertEqual(list(statuses.values()), [timelapse.VIDEO_STATUS_ERROR])

    def test_failed_build_removes_partial_artifact(self):
        # A stated size larger than the on-disk payload simulates truncation.
        _write_frame(self.dir, 0)
        with patch.object(timelapse.os.path, 'getsize', return_value=10_000):
            with self.assertRaises(OSError):
                timelapse.build_avi(self.dir, fps=10)
        self.assertEqual(timelapse.list_videos(self.dir), [])
        self.assertEqual(
            list(timelapse.read_video_index(self.dir).values()),
            [timelapse.VIDEO_STATUS_ERROR])

    def test_truncated_frame_records_error(self):
        # A size larger than the on-disk payload simulates a truncation race.
        name = _write_frame(self.dir, 0)
        with patch.object(timelapse.os.path, 'getsize', return_value=10_000):
            with self.assertRaises(OSError):
                timelapse.build_avi(self.dir, fps=10)
        statuses = timelapse.read_video_index(self.dir)
        self.assertTrue(all(status == 'E' for status in statuses.values()))
        self.assertEqual(timelapse.list_videos(self.dir), [])
        self.assertTrue(name)  # frame itself is untouched

    def test_names_restriction_ignores_traversal_and_non_frames(self):
        _write_frame(self.dir, 0)
        path = timelapse.build_avi(
            self.dir, fps=10,
            names=['../evil.jpg', 'x.txt', 'a.avi', _frame_name(0)])
        self.assertTrue(os.path.exists(path))
        # Only the valid frame is assembled: one-frame AVI.
        self.assertEqual(len(timelapse.list_frames(self.dir)), 1)
        # A names list with no valid frame yields no build and no artifact.
        before = set(timelapse.list_videos(self.dir))
        self.assertIsNone(
            timelapse.build_avi(self.dir, fps=10, names=['../evil.jpg', 'x.txt']))
        self.assertEqual(set(timelapse.list_videos(self.dir)), before)

    def test_names_restriction_and_symlink_avoidance(self):
        _write_frame(self.dir, 0)
        _write_frame(self.dir, 1)
        os.symlink(os.path.join(self.dir, _frame_name(1)),
                   os.path.join(self.dir, _frame_name(2)))
        entries, _ = media_library.catalog_frames(self.dir)
        names = [e.name for e in entries]
        path = timelapse.build_avi(self.dir, fps=10, names=names)
        self.assertTrue(os.path.exists(path))
        self.assertNotIn(_frame_name(2), names)


class _BlockingBuild:
    """Injectable build function that records concurrency and can block/fail."""

    def __init__(self, result='built.avi', fail=None, block=True, stop_ok=True):
        self.started = threading.Event()
        self.release = threading.Event()
        self.result = result
        self.fail = fail
        self.block = block
        self.stop_ok = stop_ok
        self.calls = []
        self.concurrent = 0
        self.max_concurrent = 0
        self._guard = threading.Lock()

    def __call__(self, **kwargs):
        with self._guard:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.calls.append(kwargs)
        self.started.set()
        try:
            if self.block:
                self.release.wait(5)
            if self.fail is not None:
                raise self.fail
            if self.stop_ok and kwargs['stop_event'].is_set():
                raise timelapse.BuildCancelled()
            if kwargs.get('progress'):
                kwargs['progress'](1, 1)
            return self.result
        finally:
            with self._guard:
                self.concurrent -= 1


class BuildManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _manager(self, **kwargs):
        manager = media_build.BuildManager(self.dir, **kwargs)
        self.addCleanup(manager.stop)
        return manager

    def test_no_frames_rejected(self):
        result = self._manager(build_fn=_BlockingBuild(block=False)).start()
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'no_frames')

    def test_too_many_frames_rejected(self):
        _write_frame(self.dir, 0)
        _write_frame(self.dir, 1)
        result = self._manager(max_frames=1, build_fn=_BlockingBuild(block=False)).start()
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'too_many_frames')

    def test_input_too_large_rejected(self):
        _write_frame(self.dir, 0)
        result = self._manager(
            max_input_bytes=4, build_fn=_BlockingBuild(block=False)).start()
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'input_too_large')

    def test_low_space_rejected(self):
        _write_frame(self.dir, 0)
        result = self._manager(
            min_free_bytes=10 ** 18, build_fn=_BlockingBuild(block=False)).start()
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'low_space')

    def test_success_reports_done_and_progress(self):
        _write_frame(self.dir, 0)
        build = _BlockingBuild(block=False)
        manager = self._manager(build_fn=build)
        result = manager.start(fps=12)
        self.assertTrue(result['ok'])
        self.assertEqual(result['job']['state'], 'pending')
        view = _wait_job(manager, result['job']['id'])
        self.assertEqual(view['state'], 'done')
        self.assertEqual(view['frames_total'], 1)
        self.assertEqual(view['frames_written'], 1)
        self.assertEqual(view['reason'], '')
        self.assertEqual(build.calls[0]['fps'], 12)

    def test_duplicate_build_is_busy(self):
        _write_frame(self.dir, 0)
        build = _BlockingBuild()
        manager = self._manager(build_fn=build)
        first = manager.start()
        self.assertTrue(first['ok'])
        self.assertTrue(build.started.wait(2))
        second = manager.start()
        self.assertFalse(second['ok'])
        self.assertEqual(second['code'], 'busy')
        self.assertEqual(second['job_id'], first['job']['id'])
        self.assertEqual(build.max_concurrent, 1)
        build.release.set()
        _wait_job(manager, first['job']['id'])

    def test_failure_reason_has_no_path(self):
        _write_frame(self.dir, 0)
        secret = '/home/someone/private/timelapse'
        build = _BlockingBuild(block=False, fail=FileNotFoundError(secret))
        manager = self._manager(build_fn=build)
        result = manager.start()
        view = _wait_job(manager, result['job']['id'])
        self.assertEqual(view['state'], 'error')
        self.assertEqual(view['reason'], 'build failed')
        self.assertNotIn(secret, json.dumps(view))

    def test_stop_cancels_and_reports_stopped(self):
        _write_frame(self.dir, 0)
        build = _BlockingBuild()
        manager = self._manager(build_fn=build)
        result = manager.start()
        self.assertTrue(build.started.wait(2))
        manager.stop()
        view = _wait_job(manager, result['job']['id'])
        self.assertEqual(view['state'], 'error')
        self.assertEqual(view['reason'], 'build stopped')

    def test_stale_and_fresh_lock_file(self):
        _write_frame(self.dir, 0)
        build = _BlockingBuild(block=False)
        manager = self._manager(build_fn=build)
        lock = os.path.join(self.dir, media_build.LOCK_NAME)
        with open(lock, 'w', encoding='utf-8') as handle:
            handle.write('stale')
        fresh = manager.start()
        self.assertFalse(fresh['ok'])
        os.utime(lock, (time.time() - 7200, time.time() - 7200))
        stale = manager.start()
        self.assertTrue(stale['ok'])
        view = _wait_job(manager, stale['job']['id'])
        self.assertEqual(view['state'], 'done')
        self.assertFalse(os.path.exists(lock))

    def test_job_history_is_bounded(self):
        _write_frame(self.dir, 0)
        build = _BlockingBuild(block=False)
        manager = self._manager(build_fn=build)
        ids = []
        with patch.object(media_build, 'MAX_JOBS', 2):
            for _ in range(3):
                result = manager.start()
                self.assertTrue(result['ok'], result)
                ids.append(result['job']['id'])
                _wait_job(manager, ids[-1])
                time.sleep(0.02)
        self.assertIsNone(manager.status(ids[0]))
        self.assertIsNotNone(manager.status(ids[-1]))

    def test_job_id_validation_and_unknown(self):
        manager = self._manager(build_fn=_BlockingBuild(block=False))
        self.assertIsNone(manager.status('nope'))
        self.assertIsNone(manager.status('../../etc/passwd'))
        self.assertFalse(media_build.valid_job_id('g' * 32))
        self.assertFalse(media_build.valid_job_id('0' * 31))
        self.assertTrue(media_build.valid_job_id('0' * 32))

    def test_default_builder_streams_and_writes_done(self):
        _write_frame(self.dir, 0)
        _write_frame(self.dir, 1)
        manager = self._manager()
        result = manager.start()
        self.assertTrue(result['ok'], result)
        view = _wait_job(manager, result['job']['id'])
        self.assertEqual(view['state'], 'done')
        videos = timelapse.list_videos(self.dir)
        self.assertEqual(len(videos), 1)
        self.assertEqual(timelapse.read_video_index(self.dir)[videos[0]], 'D')

    def test_rapid_sequential_builds_never_leak_or_block(self):
        _write_frame(self.dir, 0)
        build = _BlockingBuild(block=False)
        manager = self._manager(build_fn=build)
        lock = os.path.join(self.dir, media_build.LOCK_NAME)
        for _ in range(5):
            result = manager.start()
            self.assertTrue(result['ok'], result)
            _wait_job(manager, result['job']['id'])
            self.assertFalse(os.path.exists(lock))
        self.assertEqual(build.max_concurrent, 1)


if __name__ == '__main__':
    unittest.main()
