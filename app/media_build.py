"""One serialized, memory-bounded timelapse AVI build job (WP-UI6; AC-13).

The admin build route must never run two builds at once and must never let an
unbounded frame backlog exhaust a Pi Zero 2 W. This module owns that policy:

* **One job at a time.** An in-process lock plus an exclusive on-disk lock file
  (stale after a bounded age) prevents a duplicate/concurrent build even if a
  second admin process is started.
* **Preflight gates.** Frame count, aggregate input bytes computed from ``stat``
  (never by reading the JPEGs), and free space are all checked before a thread
  starts, and rejected with a bounded reason.
* **Memory-safe execution.** The build runs on its own daemon thread, off the
  asyncio loop and the single admin-core worker, and calls the streaming
  :func:`timelapse.build_avi` so only one 64 KiB chunk is resident per frame.
* **Honest, bounded status.** Only the latest few jobs are retained; a status
  response carries state, progress, timestamps, and a fixed reason string. No
  absolute path, secret, or traceback is ever exposed. Job ids are 128-bit
  random and non-enumerable; build status is deliberately visible to any
  authenticated admin session, and that global visibility is safe because the
  view is bounded and non-secret.

Firmware status characters keep their exact meaning: the build still appends
``D`` on success and ``E`` on failure to the hidden index via
:func:`timelapse.build_avi`; ``P`` is still read as pending and ``U`` as
unknown by :mod:`media_library`.
"""
import datetime
import logging
import os
import re
import threading
import time
import uuid

import media_library
import timelapse

#: Job states surfaced to the UI. ``pending``/``running`` are active.
STATE_PENDING = 'pending'
STATE_RUNNING = 'running'
STATE_DONE = 'done'
STATE_ERROR = 'error'
_ACTIVE_STATES = frozenset({STATE_PENDING, STATE_RUNNING})

#: Maximum completed jobs retained for status lookups.
MAX_JOBS = 8

#: Exclusive build lock file inside the media directory.
LOCK_NAME = '.timelapse_build.lock'

#: An unreleased lock older than this is treated as stale (crashed build).
LOCK_STALE_SECONDS = 3600.0

#: Preflight gates. These bound the work and the working set of one build.
MAX_BUILD_FRAMES = 20000
MAX_BUILD_INPUT_BYTES = 2 * 1024 * 1024 * 1024
MIN_FREE_MARGIN_BYTES = 32 * 1024 * 1024

#: Fixed, path-free failure reasons (never derived from an exception message).
REASON_NO_FRAMES = 'no frames to build'
REASON_SCAN_TRUNCATED = 'frame backlog too large to scan safely'
REASON_TOO_MANY_FRAMES = 'too many frames for one build'
REASON_INPUT_TOO_LARGE = 'input frames exceed the build size limit'
REASON_LOW_SPACE = 'insufficient free space for the build'
REASON_BUSY = 'a build is already running'
REASON_FAILED = 'build failed'
REASON_STOPPED = 'build stopped'
REASON_BAD_SESSION = 'unknown session'

#: Automatic session builds that found the manager busy are retried this often.
QUEUE_RETRY_SECONDS = 30.0
QUEUE_MAX_RETRIES = 120

log = logging.getLogger('pibuddycam.media_build')

_JOB_ID_RE = re.compile(r'^[0-9a-f]{32}$')


def valid_job_id(value):
    """True for a bounded, well-formed job id (never a path)."""
    return isinstance(value, str) and _JOB_ID_RE.match(value) is not None


def _timelapse_build(*, directory, fps, width, height, names, progress, stop_event,
                     **extra):
    """Default builder: the streaming firmware-parity AVI writer.

    ``extra`` carries ``out_dir``/``name`` for a per-print session build only.
    """
    return timelapse.build_avi(
        directory, fps=fps, width=width, height=height, names=names,
        progress=progress, stop_event=stop_event, **extra,
    )


class BuildManager:
    """Serialize one background AVI build and expose bounded job status."""

    def __init__(self, directory=media_library.DEFAULT_MEDIA_DIR, *,
                 build_fn=None, clock=time.time,
                 max_frames=MAX_BUILD_FRAMES,
                 max_input_bytes=MAX_BUILD_INPUT_BYTES,
                 min_free_bytes=MIN_FREE_MARGIN_BYTES,
                 lock_name=LOCK_NAME, lock_stale=LOCK_STALE_SECONDS):
        self._directory = directory
        self._build = build_fn or _timelapse_build
        self._clock = clock
        self._max_frames = int(max_frames)
        self._max_input_bytes = int(max_input_bytes)
        self._min_free_bytes = int(min_free_bytes)
        self._lock_path = os.path.join(directory, lock_name)
        self._lock_stale = float(lock_stale)

        self._lock = threading.Lock()
        self._jobs = {}
        self._order = []
        self._stop = threading.Event()
        self._thread = None
        self._lock_held = False
        # Session builds requested while busy; each is retried until it starts.
        self._queue = []
        self._retry_timer = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def start(self, fps=timelapse.DEFAULT_FPS, width=None, height=None, session=None):
        """Preflight and queue one build. Returns ``{ok, job}`` or ``{ok,error}``.

        Only the first call while a job is active can win; every other call gets
        :data:`REASON_BUSY` (or a specific gate reason) and never starts a
        second thread. ``session`` builds one per-print session folder into the
        timelapse root as ``<session>.avi`` instead of the root frames.
        """
        source_dir = self._directory
        extra = {}
        if session is not None:
            path = media_library.session_dir(self._directory, session)
            if path is None:
                return self._rejection(REASON_BAD_SESSION, 'bad_session')
            source_dir = path
            extra = {'out_dir': self._directory, 'name': session + timelapse.AVI_SUFFIX}
        with self._lock:
            active = self._active_job_locked()
            if active is not None:
                return {
                    'ok': False,
                    'error': REASON_BUSY,
                    'code': 'busy',
                    'job_id': active['id'],
                }

            entries, truncated = media_library.catalog_frames(source_dir)
            if truncated:
                return self._rejection(REASON_SCAN_TRUNCATED, 'too_large')
            count = len(entries)
            if count == 0:
                return self._rejection(REASON_NO_FRAMES, 'no_frames')
            if count > self._max_frames:
                return self._rejection(REASON_TOO_MANY_FRAMES, 'too_many_frames')
            total = sum(entry.size for entry in entries)
            if total > self._max_input_bytes:
                return self._rejection(REASON_INPUT_TOO_LARGE, 'input_too_large')
            free = media_library.free_bytes(self._directory)
            required = total + count * 16 + 4096 + self._min_free_bytes
            if free is None or free < required:
                return self._rejection(REASON_LOW_SPACE, 'low_space')
            if not self._acquire_lock():
                return self._rejection(REASON_BUSY, 'busy')

            job_id = uuid.uuid4().hex
            job = {
                'id': job_id,
                'state': STATE_PENDING,
                'frames_total': count,
                'frames_written': 0,
                'reason': '',
                'started': self._clock(),
                'finished': None,
            }
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._prune_locked()
            names = [entry.name for entry in entries]
            self._stop.clear()
            thread = threading.Thread(
                target=self._run,
                args=(job_id, fps, width, height, names, source_dir, extra),
                name='timelapse-build',
                daemon=True,
            )
            try:
                thread.start()
            except Exception:  # noqa: BLE001 - never leave the lock or job stuck
                self._jobs.pop(job_id, None)
                self._order = [item for item in self._order if item != job_id]
                self._release_lock_locked()
                return self._rejection(REASON_FAILED, 'failed')
            self._thread = thread
            return {'ok': True, 'job': self._view(job)}

    def status(self, job_id):
        """Return the bounded view of ``job_id``, or ``None`` when unknown.

        Job ids are 128-bit random values, so they are not enumerable or
        guessable. Build status is intentionally visible to any authenticated
        admin session (there is no per-session ownership), but a status view
        contains only the bounded state/progress/reason/timestamps — never a
        path, frame name, or secret — so that global visibility is safe.
        """
        if not valid_job_id(job_id):
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            return self._view(job) if job is not None else None

    def stop(self, timeout=2.0):
        """Request cancellation and join the build thread (idempotent)."""
        self._stop.set()
        with self._lock:
            timer, self._retry_timer = self._retry_timer, None
            self._queue.clear()
        if timer is not None:
            timer.cancel()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        return not (thread is not None and thread.is_alive())

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _run(self, job_id, fps, width, height, names, source_dir=None, extra=None):
        self._update(job_id, state=STATE_RUNNING)
        try:
            def progress(written, total):
                self._update(job_id, frames_written=written)

            path = self._build(
                directory=source_dir or self._directory, fps=fps, width=width,
                height=height, names=names, progress=progress,
                stop_event=self._stop, **(extra or {}),
            )
            if self._stop.is_set():
                final_state, reason = STATE_ERROR, REASON_STOPPED
            elif path:
                final_state, reason = STATE_DONE, ''
            else:
                final_state, reason = STATE_ERROR, REASON_NO_FRAMES
        except timelapse.BuildCancelled:
            final_state, reason = STATE_ERROR, REASON_STOPPED
        except Exception:  # noqa: BLE001 - a build must never kill the thread
            final_state, reason = STATE_ERROR, REASON_FAILED
        # The terminal state and the lock release happen in one critical
        # section: a concurrent ``start`` can never observe a finished job while
        # the old lock file is still held, so it can never release the lock of a
        # job that started after it.
        self._finish(job_id, final_state, reason)
        self._drain_queue()

    def enqueue_session(self, session, fps=timelapse.DEFAULT_FPS, width=None,
                        height=None):
        """Build ``session`` as soon as possible; never drop it silently.

        Used by the automatic build at the end of a print. When a build is
        already running (or another process holds the on-disk lock) the request
        is kept and retried when the running job finishes and on a timer.
        Returns ``True`` when it started immediately.
        """
        if media_library.session_dir(self._directory, session) is None:
            return False
        result = self.start(fps, width, height, session=session)
        if result.get('ok'):
            return True
        if result.get('code') == 'busy':
            with self._lock:
                if not any(item[0] == session for item in self._queue):
                    self._queue.append([session, fps, width, height, 0])
            self._schedule_retry()
        return False

    def _drain_queue(self):
        """Start the next queued session build, if any (outside the job lock)."""
        with self._lock:
            if not self._queue:
                return
            item = self._queue[0]
        session, fps, width, height, attempts = item
        result = self.start(fps, width, height, session=session)
        if result.get('ok') or result.get('code') != 'busy':
            # Started, or permanently rejected (no frames, low space, ...): done.
            with self._lock:
                if item in self._queue:
                    self._queue.remove(item)
            if not result.get('ok'):
                log.warning(f"queued session build dropped: {result.get('error')}")
                self._drain_queue()   # do not let one bad session block the rest
            return
        item[4] = attempts + 1
        if item[4] >= QUEUE_MAX_RETRIES:
            with self._lock:
                if item in self._queue:
                    self._queue.remove(item)
            log.warning('queued session build gave up: manager stayed busy')
            return
        self._schedule_retry()

    def _schedule_retry(self):
        with self._lock:
            if self._retry_timer is not None or not self._queue:
                return
            timer = threading.Timer(QUEUE_RETRY_SECONDS, self._retry)
            timer.daemon = True
            self._retry_timer = timer
        timer.start()

    def _retry(self):
        with self._lock:
            self._retry_timer = None
        self._drain_queue()

    def _finish(self, job_id, state, reason):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job['state'] = state
                job['reason'] = reason
                job['finished'] = self._clock()
            self._release_lock_locked()

    def _update(self, job_id, **fields):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(fields)

    def _active_job_locked(self):
        for job_id in self._order:
            job = self._jobs.get(job_id)
            if job is not None and job['state'] in _ACTIVE_STATES:
                return job
        return None

    def _prune_locked(self):
        """Drop the oldest completed jobs beyond :data:`MAX_JOBS`."""
        while len(self._order) > MAX_JOBS:
            oldest = self._order[0]
            job = self._jobs.get(oldest)
            if job is not None and job['state'] in _ACTIVE_STATES:
                break
            self._order.pop(0)
            self._jobs.pop(oldest, None)

    def _view(self, job):
        """Return a bounded copy of a job (no internal keys, no paths)."""
        if job is None:
            return None
        finished = job.get('finished')
        return {
            'id': job['id'],
            'state': job['state'],
            'frames_total': int(job.get('frames_total') or 0),
            'frames_written': int(job.get('frames_written') or 0),
            'reason': str(job.get('reason') or '')[:200],
            'started': _timestamp(job.get('started')),
            'finished': _timestamp(finished),
        }

    def _rejection(self, reason, code):
        return {'ok': False, 'error': reason, 'code': code, 'job_id': ''}

    # -- exclusive lock file ------------------------------------------- #

    def _acquire_lock(self):
        if self._lock_held:
            return False
        if not self._try_create_lock():
            if not self._lock_is_stale():
                return False
            try:
                os.unlink(self._lock_path)
            except OSError:
                return False
            if not self._try_create_lock():
                return False
        self._lock_held = True
        return True

    def _try_create_lock(self):
        try:
            fd = os.open(
                self._lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except OSError:
            return False
        try:
            os.write(fd, str(os.getpid()).encode('ascii', 'replace'))
        except OSError:
            pass
        finally:
            os.close(fd)
        return True

    def _lock_is_stale(self):
        try:
            age = time.time() - os.stat(self._lock_path).st_mtime
        except OSError:
            return True
        return age > self._lock_stale

    def _release_lock_locked(self):
        """Release the held lock file; caller must hold :attr:`_lock`."""
        if not self._lock_held:
            return
        self._lock_held = False
        try:
            os.unlink(self._lock_path)
        except OSError:
            pass


def _timestamp(value):
    """Render a clock value as a bounded ISO-8601 string, or ``None``."""
    if value is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(
            float(value), datetime.timezone.utc
        ).strftime('%Y-%m-%dT%H:%M:%SZ')
    except (TypeError, ValueError, OSError, OverflowError):
        return None
