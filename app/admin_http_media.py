"""Timelapse media library and print sessions (WP-UI6; AC-12/AC-13/AC-17/AC-18).

Handler mixin for :class:`admin_http.AdminApp`. The route table, the security
policy and the shared helpers stay in ``admin_http``; this module only holds the
handlers of one area so ``admin_http`` stays reviewable. It is imported by
``admin_http`` after those helpers exist, so it must not be imported on its own.
"""

import logging

import media_library
import timelapse

from admin_http import (  # noqa: E402 - partially initialised, see above
    MAX_MEDIA_BUILD_BODY_BYTES,
    MAX_SYSTEM_ACTION_BODY_BYTES,
    MEDIA_DEFAULT_PAGE_SIZE,
    MEDIA_JOB_UNKNOWN,
    MEDIA_MAX_SESSIONS,
    Response,
    _body_size,
    _header,
    _query_int,
)

log = logging.getLogger('pibuddycam.admin_http')


class MediaHandlers:
    """Handlers for this area; ``self`` is the ``AdminApp``."""

    # ------------------------------------------------------------------ #
    # Timelapse media library (WP-UI6; AC-12/AC-13/AC-17/AC-18)
    # ------------------------------------------------------------------ #

    def _media_page(self, request):
        """Parse strict ``page``/``page_size`` bounds, or return an error string."""
        query = request.query if isinstance(request.query, dict) else {}
        page = _query_int(query.get('page'), 1, 1, media_library.MAX_PAGE)
        if page is None:
            return None, None, 'invalid page'
        page_size = _query_int(
            query.get('page_size'), MEDIA_DEFAULT_PAGE_SIZE, 1,
            media_library.MAX_PAGE_SIZE,
        )
        if page_size is None:
            return None, None, 'invalid page_size'
        return page, page_size, None

    def _media_status_filter(self, request):
        """Return ``(raw_char_or_None, error)`` for the ``status`` query filter.

        Accepts the UI label (``completed``/``error``/``pending``/``unknown``),
        the raw firmware char (``D``/``E``/``P``/``U``), or ``all``/absent.
        """
        query = request.query if isinstance(request.query, dict) else {}
        raw = query.get('status')
        if raw is None or raw == '' or raw == 'all':
            return None, None
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not isinstance(raw, str):
            return None, 'invalid status'
        if raw in media_library.STATUS_CHARS:
            return media_library.STATUS_CHARS[raw], None
        if raw in media_library.STATUS_LABELS:
            return raw, None
        return None, 'invalid status'

    def _handle_media_videos(self, request, match, body_data, now):
        """Paginated metadata for allowlisted ``.avi`` artifacts (AC-12).

        Only stat metadata is returned: name, size, mtime, and the raw
        ``D/E/P/U`` index char mapped to a label. No path, index contents, or
        file bytes appear. Pagination is strict and the scan is capped.
        """
        page, page_size, error = self._media_page(request)
        if error:
            return self._error(request, 400, error)
        status_filter, error = self._media_status_filter(request)
        if error:
            return self._error(request, 400, error)
        entries, truncated = media_library.catalog_videos(self._media_dir)
        payload = {'ok': True, 'kind': 'videos', 'truncated': truncated}
        payload['bytes_total'] = sum(entry.size for entry in entries)
        payload['status_counts'] = media_library.status_counts(entries)
        if status_filter is not None:
            entries = [entry for entry in entries if entry.status == status_filter]
        payload.update(media_library.paginate(entries, page, page_size))
        for item in payload['items']:
            item['download_url'] = f"/api/media/timelapses/{item['name']}"
        return self._json(request, 200, payload)

    def _media_session_dir(self, request):
        """Return ``(directory, session, error)`` for the optional ``session`` query.

        Absent means the timelapse root. A session must match
        ``session_<YYYYMMDD>-<HHMMSS>`` and be a real folder under the media root.
        """
        query = request.query if isinstance(request.query, dict) else {}
        raw = query.get('session')
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if raw is None or raw == '':
            return self._media_dir, None, None
        if not isinstance(raw, str) or not timelapse.valid_session_name(raw):
            return None, None, 'invalid session'
        path = media_library.session_dir(self._media_dir, raw)
        if path is None:
            return None, None, 'unknown session'
        return path, raw, None

    def _handle_media_frames(self, request, match, body_data, now):
        """Paginated metadata for allowlisted stored ``.jpg`` frames (AC-12).

        An optional ``session=`` query lists one per-print session's frames.
        """
        page, page_size, error = self._media_page(request)
        if error:
            return self._error(request, 400, error)
        directory, session, error = self._media_session_dir(request)
        if error:
            return self._error(request, 400 if error == 'invalid session' else 404, error)
        entries, truncated = media_library.catalog_frames(directory)
        payload = {'ok': True, 'kind': 'frames', 'truncated': truncated}
        if session:
            payload['session'] = session
        payload['bytes_total'] = sum(entry.size for entry in entries)
        payload.update(media_library.paginate(entries, page, page_size))
        suffix = f'?session={session}' if session else ''
        for item in payload['items']:
            item['preview_url'] = f"/api/media/frames/{item['name']}{suffix}"
            item['download_url'] = item['preview_url']
        return self._json(request, 200, payload)

    def _handle_media_sessions(self, request, match, body_data, now):
        """Per-print timelapse sessions (newest first, bounded)."""
        sessions, truncated = media_library.catalog_sessions(self._media_dir)
        return self._json(request, 200, {
            'ok': True,
            'kind': 'sessions',
            'truncated': truncated,
            'active': timelapse.active_session(self._media_dir),
            'sessions': sessions[:MEDIA_MAX_SESSIONS],
        })

    def _handle_media_session_delete(self, request, match, body_data, now):
        """Delete one finished print session (frames, optionally its video).

        Fresh re-auth window + CSRF (route policy) and an explicit
        ``confirm: true``. The session must be a real ``session_<stamp>`` folder;
        the open recording session and any time a build is running are refused
        (409), a folder holding anything but timelapse frames is refused (422).
        Loose frames and videos in the root are never deletable here.
        """
        if _body_size(request) > MAX_SYSTEM_ACTION_BODY_BYTES:
            return self._error(request, 413, 'request body too large')
        if not isinstance(body_data, dict):
            return self._error(request, 400, 'invalid request body')
        if any(key not in ('confirm', 'delete_video') for key in body_data):
            return self._error(request, 400, 'unknown field')
        if body_data.get('confirm') is not True:
            return self._json(request, 400, {
                'ok': False, 'error': 'explicit confirmation is required'})
        delete_video = body_data.get('delete_video', False)
        if type(delete_video) is not bool:
            return self._error(request, 400, 'delete_video must be a boolean')
        name = match.group('name')
        if not timelapse.valid_session_name(name):
            return self._error(request, 400, 'invalid session')
        if media_library.build_running(self._media_dir):
            return self._json(request, 409, {
                'ok': False, 'error': 'a build is running; try again when it has finished'})
        try:
            result = timelapse.delete_session(self._media_dir, name, delete_video)
        except timelapse.SessionError as exc:
            status = {'not_found': 404, 'active': 409, 'bad_name': 400,
                      'unexpected_content': 422}.get(exc.code, 400)
            return self._json(request, status, {'ok': False, 'error': exc.message})
        except OSError:
            log.warning('admin_http: session delete failed')
            return self._error(request, 500, 'the session could not be deleted')
        return self._json(request, 200, {'ok': True, 'session': name, **result})

    def _handle_media_video_download(self, request, match, body_data, now):
        """Serve one allowlisted AVI with a single byte range (AC-12).

        The core opens the file with ``O_NOFOLLOW`` and revalidates it with
        ``fstat`` before any header is produced. It parses exactly one
        ``bytes=`` range, answers 206/200 with a bounded descriptor, and hands
        the transport an already-positioned file so the AVI is streamed in
        chunks, never read whole.
        """
        name = match.group('name')
        try:
            handle, size, _kind = media_library.open_media(
                self._media_dir, name, expect='video')
        except media_library.MediaError as exc:
            return self._error(request, exc.status, exc.message)
        try:
            span = media_library.parse_range(
                _header(request.headers, 'Range'), size)
        except media_library.RangeNotSatisfiable:
            handle.close()
            response = self._json(request, 416, {'ok': False, 'error': 'range not satisfiable'})
            response.headers['Content-Range'] = f'bytes */{size}'
            response.headers['Accept-Ranges'] = 'bytes'
            response.headers['Cache-Control'] = 'private, no-store'
            return response

        if span is None:
            status, start, end = 200, 0, (size - 1 if size else 0)
        else:
            status, (start, end) = 206, span
        length = (end - start + 1) if size else 0
        headers = {
            'Content-Type': 'video/x-msvideo',
            'Content-Disposition': f'attachment; filename="{name}"',
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'private, no-store',
            'Content-Length': str(length),
        }
        if status == 206:
            headers['Content-Range'] = f'bytes {start}-{end}/{size}'
        try:
            handle.seek(start)
        except OSError:
            handle.close()
            return self._error(request, 500, 'media unavailable')
        return Response(
            status, headers, b'', file=handle, file_offset=0, file_length=length)

    def _handle_media_frame_download(self, request, match, body_data, now):
        """Serve one allowlisted, bounded JPEG frame as a preview (AC-12).

        The frame must be a regular, non-symlink file within the size bound and
        carry a valid JPEG start-of-image prefix; it is streamed, so even a
        planted near-limit JPEG is never decoded or buffered whole.
        """
        name = match.group('name')
        directory, _session, error = self._media_session_dir(request)
        if error:
            return self._error(request, 404, 'not found')
        try:
            handle, size, _kind = media_library.open_media(
                directory, name, expect='frame')
        except media_library.MediaError as exc:
            return self._error(request, exc.status, exc.message)
        try:
            head = handle.read(3)
            handle.seek(0)
        except OSError:
            handle.close()
            return self._error(request, 500, 'media unavailable')
        if not media_library.valid_jpeg_head(head):
            handle.close()
            return self._error(request, 404, 'not found')
        headers = {
            'Content-Type': 'image/jpeg',
            'Content-Disposition': f'inline; filename="{name}"',
            'Cache-Control': 'private, max-age=300',
            'Content-Length': str(size),
        }
        return Response(200, headers, b'', file=handle, file_offset=0, file_length=size)

    def _handle_media_build(self, request, match, body_data, now):
        """Queue one serialized, gated background AVI build (AC-13).

        Authenticated + CSRF (POST policy). The injected manager owns the single
        job and the preflight memory/frame/space gates; a duplicate request is a
        bounded 409 and a gate failure a bounded 422/507. The handler never runs
        the build itself.
        """
        if _body_size(request) > MAX_MEDIA_BUILD_BODY_BYTES:
            return self._error(request, 413, 'request body too large')
        body = body_data if isinstance(body_data, dict) else {}
        unknown = [key for key in body if key not in ('fps', 'session')]
        if unknown:
            return self._error(request, 400, 'unknown field')
        session = body.get('session')
        if session is not None and not timelapse.valid_session_name(session):
            return self._error(request, 400, 'invalid session')
        fps = body.get('fps', timelapse.DEFAULT_FPS) if 'fps' in body else timelapse.DEFAULT_FPS
        valid_fps = timelapse.valid_fps(fps)
        if valid_fps is None:
            return self._error(request, 400, 'invalid fps')
        manager = self._build_manager
        if manager is None:
            return self._error(request, 503, 'build unavailable')
        result = (
            manager.start(valid_fps, session=session) if session
            else manager.start(valid_fps))
        if not result.get('ok'):
            code = result.get('code')
            status = 409 if code == 'busy' else (507 if code == 'low_space' else 422)
            return self._json(request, status, {
                'ok': False,
                'error': result.get('error') or 'build rejected',
                'job_id': result.get('job_id') or '',
            })
        return self._json(request, 202, {'ok': True, 'job': result['job']})

    def _handle_media_job(self, request, match, body_data, now):
        """Return bounded status for one build job id (AC-13).

        Job ids are 128-bit random values, so a client cannot enumerate them.
        Visibility is intentionally global to any authenticated admin session;
        the bounded view carries only state/progress/fixed reason/timestamps
        (no path, name, or secret), so that is safe.
        """
        manager = self._build_manager
        if manager is None:
            return self._error(request, 503, 'build unavailable')
        job_id = match.group('id')
        view = manager.status(job_id)
        if view is None:
            return self._error(request, 404, MEDIA_JOB_UNKNOWN)
        return self._json(request, 200, {'ok': True, 'job': view})
