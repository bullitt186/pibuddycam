"""Per-print timelapse sessions: folders, storage, builds, catalog and API."""
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import admin_auth  # noqa: E402
import admin_http  # noqa: E402
import media_build  # noqa: E402
import media_library  # noqa: E402
import provisioning  # noqa: E402
import timelapse  # noqa: E402

ADMIN_PASSWORD = 'correct horse battery staple'
ADMIN_HASH = admin_auth.hash_password(ADMIN_PASSWORD)
NOW = 1000.0


def jpeg(width=4, height=4):
    sof = (b'\xff\xc0' + struct.pack('>H', 17) + b'\x08'
           + struct.pack('>HH', height, width) + b'\x03' + b'\x01\x11\x00' * 3)
    return b'\xff\xd8' + sof + b'\xff\xd9'


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def put_frames(self, session, count=2):
        path = os.path.join(self.dir, session) if session else self.dir
        os.makedirs(path, exist_ok=True)
        names = []
        for index in range(count):
            name = f'timelapse_00-00-{index:02d}-000.jpg'
            with open(os.path.join(path, name), 'wb') as handle:
                handle.write(jpeg())
            names.append(name)
        return names


class SessionFolderTests(Tmp):
    def test_open_creates_folder_and_marker(self):
        name = timelapse.open_session(self.dir, now=1_700_000_000)
        self.assertRegex(name, r'^session_[0-9]{8}-[0-9]{6}$')
        self.assertTrue(os.path.isdir(os.path.join(self.dir, name)))
        self.assertEqual(timelapse.active_session(self.dir), name)
        with open(os.path.join(self.dir, timelapse.SESSION_MARKER), encoding='utf-8') as f:
            self.assertEqual(f.read().strip(), name)

    def test_stale_clock_can_never_collide(self):
        # No RTC: two sessions opened "at the same second" get distinct names, and
        # a built video with the same stamp is never shadowed either.
        first = timelapse.open_session(self.dir, now=1_700_000_000)
        timelapse.close_session(self.dir)
        second = timelapse.open_session(self.dir, now=1_700_000_000)
        self.assertNotEqual(first, second)
        timelapse.close_session(self.dir)
        with open(os.path.join(self.dir, second + '.avi'), 'wb'):
            pass
        os.rmdir(os.path.join(self.dir, second))
        third = timelapse.open_session(self.dir, now=1_700_000_000)
        self.assertNotIn(third, (first, second))
        self.assertTrue(os.path.isdir(os.path.join(self.dir, first)))

    def test_close_returns_the_name_and_drops_the_marker(self):
        name = timelapse.open_session(self.dir, now=1_700_000_000)
        self.assertEqual(timelapse.close_session(self.dir), name)
        self.assertIsNone(timelapse.active_session(self.dir))
        self.assertIsNone(timelapse.close_session(self.dir))

    def test_marker_pointing_nowhere_or_at_garbage_is_no_session(self):
        marker = os.path.join(self.dir, timelapse.SESSION_MARKER)
        for content in ('session_20260101-000000\n', '../etc\n', 'session_x\n', ''):
            with open(marker, 'w', encoding='utf-8') as handle:
                handle.write(content)
            self.assertIsNone(timelapse.active_session(self.dir), repr(content))

    def test_marker_pointing_at_a_symlink_is_no_session(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        os.symlink(outside.name, os.path.join(self.dir, 'session_20260101-000000'))
        with open(os.path.join(self.dir, timelapse.SESSION_MARKER), 'w') as handle:
            handle.write('session_20260101-000000\n')
        self.assertIsNone(timelapse.active_session(self.dir))
        self.assertEqual(timelapse.list_sessions(self.dir), [])

    def test_frame_dir_is_the_session_only_while_open(self):
        self.assertEqual(timelapse.session_frame_dir(self.dir), self.dir)
        name = timelapse.open_session(self.dir, now=1_700_000_000)
        self.assertEqual(timelapse.session_frame_dir(self.dir), os.path.join(self.dir, name))
        timelapse.close_session(self.dir)
        self.assertEqual(timelapse.session_frame_dir(self.dir), self.dir)

    def test_save_frame_writes_into_the_session(self):
        name = timelapse.open_session(self.dir, now=1_700_000_000)
        path = timelapse.save_frame(jpeg(), dir=timelapse.session_frame_dir(self.dir))
        self.assertEqual(os.path.dirname(path), os.path.join(self.dir, name))
        self.assertEqual(timelapse.list_frames(self.dir), [])     # root stays clean

    def test_valid_session_names(self):
        self.assertTrue(timelapse.valid_session_name('session_20260101-000000'))
        for bad in ('session_2026-01-01', 'session_20260101-000000/..', '../session_20260101-000000',
                    'Session_20260101-000000', 'session_20260101-000000.avi', None, 5, ''):
            self.assertFalse(timelapse.valid_session_name(bad), repr(bad))

    def test_list_sessions_only_real_directories(self):
        self.put_frames('session_20260101-000000')
        with open(os.path.join(self.dir, 'session_20260102-000000'), 'w'):
            pass                                                     # a file
        self.assertEqual(timelapse.list_sessions(self.dir), ['session_20260101-000000'])


class BuildIntoRootTests(Tmp):
    def test_session_video_lands_in_the_root_and_is_listed(self):
        session = 'session_20260101-000000'
        self.put_frames(session, 3)
        path = timelapse.build_avi(
            os.path.join(self.dir, session), fps=5, out_dir=self.dir,
            name=session + '.avi')
        self.assertEqual(path, os.path.join(self.dir, session + '.avi'))
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(4), b'RIFF')
        self.assertEqual(os.listdir(os.path.join(self.dir, session)).count(session + '.avi'), 0)
        # firmware-shaped file list (root .avi only) and the index include it
        self.assertEqual(timelapse.file_list_entries(self.dir), f'{session}.avi;D\n')
        self.assertEqual(timelapse.read_video_index(self.dir), {session + '.avi': 'D'})
        self.assertFalse(os.path.exists(os.path.join(self.dir, session, timelapse.CSV_NAME)))
        # frames are never deleted after a successful build
        self.assertEqual(len(timelapse.list_frames(os.path.join(self.dir, session))), 3)

    def test_taken_name_falls_back_to_the_firmware_name(self):
        session = 'session_20260101-000000'
        self.put_frames(session, 1)
        with open(os.path.join(self.dir, session + '.avi'), 'wb') as handle:
            handle.write(b'existing')
        path = timelapse.build_avi(
            os.path.join(self.dir, session), out_dir=self.dir, name=session + '.avi')
        self.assertTrue(os.path.basename(path).startswith(timelapse.FRAME_PREFIX))
        with open(os.path.join(self.dir, session + '.avi'), 'rb') as handle:
            self.assertEqual(handle.read(), b'existing')

    def test_hostile_name_is_ignored(self):
        session = 'session_20260101-000000'
        self.put_frames(session, 1)
        path = timelapse.build_avi(
            os.path.join(self.dir, session), out_dir=self.dir, name='../evil.avi')
        self.assertEqual(os.path.dirname(path), self.dir)
        self.assertNotIn('evil', os.path.basename(path))

    def test_root_only_builds_are_unchanged(self):
        self.put_frames(None, 2)
        path = timelapse.build_avi(self.dir)
        self.assertEqual(os.path.dirname(path), self.dir)
        self.assertTrue(os.path.basename(path).startswith('timelapse_'))


class CatalogTests(Tmp):
    def test_sessions_catalog(self):
        self.put_frames('session_20260101-000000', 3)
        self.put_frames('session_20260102-000000', 1)
        with open(os.path.join(self.dir, 'session_20260101-000000.avi'), 'wb') as handle:
            handle.write(b'RIFF')
        self.put_frames(None, 2)                       # root frames are not a session
        sessions, truncated = media_library.catalog_sessions(self.dir)
        self.assertFalse(truncated)
        self.assertEqual([s['name'] for s in sessions],
                         ['session_20260102-000000', 'session_20260101-000000'])
        old = sessions[1]
        self.assertEqual((old['frames'], old['video']), (3, 'session_20260101-000000.avi'))
        self.assertGreater(old['bytes'], 0)
        self.assertIsNone(sessions[0]['video'])

    def test_session_dir_policy(self):
        self.put_frames('session_20260101-000000')
        self.assertEqual(media_library.session_dir(self.dir, 'session_20260101-000000'),
                         os.path.join(self.dir, 'session_20260101-000000'))
        self.assertIsNone(media_library.session_dir(self.dir, 'session_20260102-000000'))
        self.assertIsNone(media_library.session_dir(self.dir, '../x'))
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        os.symlink(outside.name, os.path.join(self.dir, 'session_20260103-000000'))
        self.assertIsNone(media_library.session_dir(self.dir, 'session_20260103-000000'))

    def test_session_video_names_pass_the_media_policy(self):
        self.assertEqual(media_library.classify_name('session_20260101-000000.avi'), 'video')

    def test_session_folders_are_not_frames_or_videos(self):
        self.put_frames('session_20260101-000000')
        self.assertEqual(media_library.catalog_frames(self.dir)[0], [])
        self.assertEqual(media_library.catalog_videos(self.dir)[0], [])


def wait_state(manager, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = manager.status(job_id)
        if view and view['state'] not in ('pending', 'running'):
            return view
        time.sleep(0.01)
    raise AssertionError('job did not finish')


class SessionBuildTests(Tmp):
    def manager(self, **kw):
        manager = media_build.BuildManager(self.dir, **kw)
        self.addCleanup(manager.stop)
        return manager

    def test_session_build_uses_session_frames_and_root_output(self):
        session = 'session_20260101-000000'
        self.put_frames(session, 2)
        self.put_frames(None, 5)                        # unrelated root frames
        manager = self.manager()
        result = manager.start(session=session)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['job']['frames_total'], 2)
        self.assertEqual(wait_state(manager, result['job']['id'])['state'], 'done')
        self.assertTrue(os.path.exists(os.path.join(self.dir, session + '.avi')))
        self.assertEqual(timelapse.read_video_index(self.dir)[session + '.avi'], 'D')

    def test_unknown_or_hostile_session_is_rejected(self):
        manager = self.manager()
        for session in ('session_20260101-000000', '../x', 'session_x'):
            result = manager.start(session=session)
            self.assertFalse(result['ok'], session)
            self.assertEqual(result['code'], 'bad_session')

    def test_root_build_is_unchanged(self):
        self.put_frames(None, 2)
        manager = self.manager()
        result = manager.start()
        self.assertEqual(wait_state(manager, result['job']['id'])['state'], 'done')

    def test_busy_session_build_is_queued_and_started_when_the_first_finishes(self):
        first, second = 'session_20260101-000000', 'session_20260102-000000'
        self.put_frames(first, 1)
        self.put_frames(second, 1)
        gate = threading.Event()
        built = []

        def build_fn(**kwargs):
            built.append(kwargs.get('name'))
            gate.wait(5)
            return os.path.join(self.dir, kwargs.get('name') or 'x.avi')

        manager = self.manager(build_fn=build_fn)
        self.assertTrue(manager.enqueue_session(first))
        self.assertFalse(manager.enqueue_session(second))       # busy: queued, not dropped
        self.assertEqual(len(manager._queue), 1)
        gate.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(built) < 2:
            time.sleep(0.01)
        self.assertEqual(built, [first + '.avi', second + '.avi'])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and manager._queue:
            time.sleep(0.01)
        self.assertEqual(manager._queue, [])

    def test_the_same_session_is_queued_only_once(self):
        first, second = 'session_20260101-000000', 'session_20260102-000000'
        self.put_frames(first, 1)
        self.put_frames(second, 1)
        gate = threading.Event()
        manager = self.manager(build_fn=lambda **kw: gate.wait(5) or None)
        manager.enqueue_session(first)
        manager.enqueue_session(second)
        manager.enqueue_session(second)
        self.assertEqual(len(manager._queue), 1)
        gate.set()

    def test_permanently_rejected_queued_session_does_not_block_the_rest(self):
        first, empty, third = ('session_20260101-000000', 'session_20260102-000000',
                               'session_20260103-000000')
        self.put_frames(first, 1)
        os.makedirs(os.path.join(self.dir, empty))              # no frames: 'no_frames'
        self.put_frames(third, 1)
        gate = threading.Event()
        built = []

        def build_fn(**kwargs):
            built.append(kwargs.get('name'))
            gate.wait(5)
            return os.path.join(self.dir, kwargs['name'])

        manager = self.manager(build_fn=build_fn)
        manager.enqueue_session(first)
        manager.enqueue_session(empty)
        manager.enqueue_session(third)
        gate.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and third + '.avi' not in built:
            time.sleep(0.01)
        self.assertEqual(built, [first + '.avi', third + '.avi'])

    def test_enqueue_of_an_unknown_session_returns_false_and_queues_nothing(self):
        manager = self.manager()
        self.assertFalse(manager.enqueue_session('session_20260101-000000'))
        self.assertEqual(manager._queue, [])

    def test_stop_cancels_the_retry_timer_and_clears_the_queue(self):
        first, second = 'session_20260101-000000', 'session_20260102-000000'
        self.put_frames(first, 1)
        self.put_frames(second, 1)
        gate = threading.Event()
        manager = self.manager(build_fn=lambda **kw: gate.wait(5) or None)
        manager.enqueue_session(first)
        manager.enqueue_session(second)
        self.assertIsNotNone(manager._retry_timer)
        gate.set()
        manager.stop()
        self.assertEqual(manager._queue, [])
        self.assertIsNone(manager._retry_timer)


class ApiTests(Tmp):
    """GET /api/media/sessions, frames?session=, build with a session."""

    class FakeBuild:
        def __init__(self):
            self.calls = []

        def start(self, fps=10, width=None, height=None, session=None):
            self.calls.append((fps, session))
            return {'ok': True, 'job': {'id': '0' * 32, 'state': 'pending'}}

        def status(self, job_id):
            return None

        def stop(self, *a, **k):
            return True

    def setUp(self):
        super().setUp()
        self.media = os.path.join(self.dir, 'media')
        os.makedirs(self.media)
        self.session = 'session_20260101-000000'
        for index in range(3):
            path = os.path.join(self.media, self.session,
                                f'timelapse_00-00-{index:02d}-000.jpg')
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as handle:
                handle.write(jpeg())
        with open(os.path.join(self.media, 'timelapse_00-00-09-000.jpg'), 'wb') as handle:
            handle.write(jpeg())
        self.build = self.FakeBuild()
        self.app = admin_http.AdminApp(
            mode='admin', sessions=admin_auth.SessionStore(),
            limiter=admin_auth.LoginRateLimiter(max_attempts=3, window=300.0, lockout=60.0),
            admin_hash=ADMIN_HASH,
            provisioning_state=provisioning.ProvisioningState(state='claimed'),
            provisioning_path=os.path.join(self.dir, 'p.json'),
            device_path=os.path.join(self.dir, 'd.toml'),
            secrets_path=os.path.join(self.dir, 's.toml'),
            clock=lambda: NOW, media_dir=self.media, build_manager=self.build)
        response = self.app.handle(admin_http.Request(
            'POST', '/api/login', {}, {'Content-Type': 'application/json'},
            json.dumps({'password': ADMIN_PASSWORD}).encode(), '10.0.0.5', NOW))
        for part in response.headers['Set-Cookie'].split(';'):
            name, _, value = part.strip().partition('=')
            if name == admin_auth.SESSION_COOKIE_NAME:
                self.token = value
        self.csrf = json.loads(response.body)['csrf']

    def call(self, method, path, query=None, body=None):
        headers = {'Cookie': f'{admin_auth.SESSION_COOKIE_NAME}={self.token}',
                   'X-CSRF-Token': self.csrf, 'Content-Type': 'application/json'}
        raw = json.dumps(body).encode() if body is not None else b''
        return self.app.handle(admin_http.Request(
            method, path, query or {}, headers, raw, '10.0.0.5', NOW))

    def test_sessions_route(self):
        body = json.loads(self.call('GET', '/api/media/sessions').body)
        self.assertEqual(body['sessions'][0]['name'], self.session)
        self.assertEqual(body['sessions'][0]['frames'], 3)
        self.assertIsNone(body['active'])

    def test_sessions_route_requires_authentication(self):
        response = self.app.handle(admin_http.Request(
            'GET', '/api/media/sessions', {}, {}, b'', '10.0.0.5', NOW))
        self.assertEqual(response.status, 401)

    def test_frames_default_to_the_root_and_session_selects_a_folder(self):
        root = json.loads(self.call('GET', '/api/media/frames').body)
        self.assertEqual(root['total'], 1)
        inside = json.loads(self.call(
            'GET', '/api/media/frames', query={'session': self.session}).body)
        self.assertEqual(inside['total'], 3)
        self.assertEqual(inside['session'], self.session)
        self.assertIn(f'?session={self.session}', inside['items'][0]['preview_url'])

    def test_bad_or_unknown_session_is_rejected(self):
        for session, status in (('../etc', 400), ('session_x', 400),
                                ('session_20990101-000000', 404)):
            response = self.call('GET', '/api/media/frames', query={'session': session})
            self.assertEqual(response.status, status, session)

    def test_frame_download_from_a_session(self):
        name = 'timelapse_00-00-01-000.jpg'
        ok = self.call('GET', f'/api/media/frames/{name}', query={'session': self.session})
        self.addCleanup(ok.file.close)
        self.assertEqual(ok.status, 200)
        self.assertEqual(ok.headers['Content-Type'], 'image/jpeg')
        # the root has no such frame
        self.assertEqual(self.call('GET', f'/api/media/frames/{name}').status, 404)
        # traversal through the session parameter never leaves the media root
        bad = self.call('GET', f'/api/media/frames/{name}', query={'session': '../media'})
        self.assertEqual(bad.status, 404)

    def test_build_with_a_session(self):
        response = self.call('POST', '/api/media/timelapses/build',
                             body={'fps': 12, 'session': self.session})
        self.assertEqual(response.status, 202)
        self.assertEqual(self.build.calls, [(12, self.session)])
        self.assertEqual(self.call('POST', '/api/media/timelapses/build',
                                   body={'session': '../x'}).status, 400)
        self.assertEqual(self.call('POST', '/api/media/timelapses/build',
                                   body={'session': 5}).status, 400)
        self.call('POST', '/api/media/timelapses/build', body={})
        self.assertEqual(self.build.calls[-1], (timelapse.DEFAULT_FPS, None))


if __name__ == '__main__':
    unittest.main()
