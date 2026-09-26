"""Safe local timelapse media catalog, path policy, and HTTP range parsing.

WP-UI6 (plan AC-12/AC-13/AC-17/AC-18). This is the stdlib-only policy layer
between the timelapse directory and the admin core:

* **Names, never paths.** A media name must be a bare basename from a strict
  character/extension allowlist; separators, dot paths, hidden names, absolute
  paths, alternate case, and trailing components are rejected outright.
* **Regular files only.** Directory enumeration uses ``scandir`` +
  ``stat(follow_symlinks=False)`` so symlinks and non-regular entries never
  reach a listing, and every open uses ``O_NOFOLLOW`` followed by ``fstat``
  revalidation, so a symlink swap between enumeration and open cannot escape
  the directory.
* **Bounded work.** A scan stops at :data:`MAX_SCAN_ENTRIES` and reports
  truncation honestly instead of reading an unbounded directory on each request.
* **Single byte ranges.** :func:`parse_range` accepts exactly one ``bytes=``
  range and raises :class:`RangeNotSatisfiable` for malformed, multiple, or
  unsatisfiable requests.

The hidden ``.timelapse_videos.csv`` index is never listed or served: its name
is hidden (leading dot) and is not an allowlisted media name. Firmware-facing
``timelapse`` behavior is untouched; this module only reads it.
"""
import os
import re
import stat

import timelapse

#: Default media root (the emulated SD timelapse directory).
DEFAULT_MEDIA_DIR = timelapse.TIMELAPSE_DIR

#: Longest accepted media name. The firmware names are ~30 characters.
MAX_NAME_CHARS = 128

#: Maximum directory entries inspected per request. A larger backlog is
#: reported as truncated rather than scanned in full on every poll.
MAX_SCAN_ENTRIES = 4096

#: Pagination bounds (strictly enforced by the admin core).
MAX_PAGE_SIZE = 100
MAX_PAGE = 10000

#: Largest JPEG served as a preview. Pi captures are far smaller; this only
#: stops a planted oversized file from being read/decoded.
MAX_JPEG_BYTES = 8 * 1024 * 1024

#: Largest AVI served; larger files are refused rather than streamed.
MAX_VIDEO_BYTES = 4 * 1024 * 1024 * 1024

#: Allowed characters in a media basename. This excludes every path separator,
#: NUL/control byte, whitespace, and non-ASCII byte in one rule.
_SAFE_CHARS = re.compile(r'^[A-Za-z0-9._-]+$')

_FRAME_NAME = re.compile(r'^timelapse_[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{3}\.jpg$')
_VIDEO_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*\.avi$')

#: Raw firmware index chars -> UI label. Only these four are meaningful; any
#: other byte is reported as ``unknown`` and never invented.
STATUS_COMPLETED = 'completed'
STATUS_ERROR = 'error'
STATUS_PENDING = 'pending'
STATUS_UNKNOWN = 'unknown'
STATUS_LABELS = {
    timelapse.VIDEO_STATUS_DONE: STATUS_COMPLETED,
    timelapse.VIDEO_STATUS_ERROR: STATUS_ERROR,
    timelapse.VIDEO_STATUS_PENDING: STATUS_PENDING,
    timelapse.VIDEO_STATUS_UNKNOWN: STATUS_UNKNOWN,
}
STATUS_CHARS = {
    STATUS_COMPLETED: timelapse.VIDEO_STATUS_DONE,
    STATUS_ERROR: timelapse.VIDEO_STATUS_ERROR,
    STATUS_PENDING: timelapse.VIDEO_STATUS_PENDING,
    STATUS_UNKNOWN: timelapse.VIDEO_STATUS_UNKNOWN,
}


class MediaError(Exception):
    """A media open/serve failure carrying an HTTP status and safe message."""

    def __init__(self, message='not found', status=404):
        super().__init__(message)
        self.message = message
        self.status = status


class RangeNotSatisfiable(Exception):
    """Raised for a malformed, multiple, or unsatisfiable ``Range`` header."""


class MediaEntry:
    """One catalogued regular media file (metadata only; never its bytes)."""

    __slots__ = ('name', 'size', 'modified', 'status')

    def __init__(self, name, size, modified, status=None):
        self.name = name
        self.size = int(size)
        self.modified = float(modified)
        self.status = status

    def as_dict(self):
        """Return a bounded JSON-safe item document (no paths)."""
        item = {
            'name': self.name,
            'size': self.size,
            'modified': round(self.modified, 3),
        }
        if self.status is not None:
            item['status'] = self.status
            item['status_label'] = status_label(self.status)
        return item


def classify_name(name):
    """Return ``'video'``, ``'frame'``, or ``None`` for a media basename.

    This is the entire path policy: a name must be a bare basename made only of
    the allowlisted characters and match the exact extension/case policy.
    """
    if not isinstance(name, str) or not name or len(name) > MAX_NAME_CHARS:
        return None
    if name != os.path.basename(name):
        return None
    if name.startswith('.') or '/' in name or '\\' in name:
        return None
    if _SAFE_CHARS.match(name) is None:
        return None
    if _FRAME_NAME.match(name) is not None:
        return 'frame'
    if _VIDEO_NAME.match(name) is not None:
        return 'video'
    return None


def status_label(status):
    """Map one raw index char to ``completed``/``error``/``pending``/``unknown``."""
    return STATUS_LABELS.get(status, STATUS_UNKNOWN)


def status_char(label):
    """Map a UI status label back to its raw index char, or ``None`` for 'all'."""
    if label == 'all':
        return None
    return STATUS_CHARS.get(label)


def status_counts(entries):
    """Return ``{label: count}`` over ``entries`` (only the four known labels)."""
    counts = {
        STATUS_COMPLETED: 0,
        STATUS_ERROR: 0,
        STATUS_PENDING: 0,
        STATUS_UNKNOWN: 0,
    }
    for entry in entries:
        counts[status_label(entry.status)] += 1
    return counts


def _scan(directory, kind):
    """Return ``(entries, truncated)`` for regular files of ``kind``.

    Never follows a symlink and never enters a subdirectory. Work is capped at
    :data:`MAX_SCAN_ENTRIES` **directory entries inspected** (not just matches),
    so a mount full of unrelated names cannot force an unbounded scan;
    ``truncated`` is true when more entries remain unread. A regular file with
    more than one hard link is excluded so a planted link from outside the
    media directory is never advertised or served.
    """
    entries = []
    truncated = False
    inspected = 0
    try:
        iterator = os.scandir(directory)
    except OSError:
        return [], False
    with iterator:
        for dirent in iterator:
            if inspected >= MAX_SCAN_ENTRIES:
                truncated = True
                break
            inspected += 1
            if classify_name(dirent.name) != kind:
                continue
            try:
                info = dirent.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                continue
            entries.append(MediaEntry(dirent.name, info.st_size, info.st_mtime))
    return entries, truncated


def _attach_status(entries, directory):
    """Fill each video entry's raw index char from the hidden index.

    A name absent from the index, or a row whose first character is not one of
    the firmware ``D/E/P/U`` chars, is normalized to ``U`` so metadata can only
    ever expose the four documented states.
    """
    statuses = timelapse.read_video_index(directory)
    for entry in entries:
        raw = statuses.get(entry.name, timelapse.VIDEO_STATUS_UNKNOWN)
        entry.status = (
            raw if raw in STATUS_LABELS else timelapse.VIDEO_STATUS_UNKNOWN)


def catalog_videos(directory=DEFAULT_MEDIA_DIR):
    """Return ``(entries, truncated)`` for allowlisted ``.avi`` files.

    Newest first (mtime, then name, descending) so the gallery shows the most
    recent build without the caller re-sorting.
    """
    entries, truncated = _scan(directory, 'video')
    _attach_status(entries, directory)
    entries.sort(key=lambda e: (e.modified, e.name), reverse=True)
    return entries, truncated


def catalog_frames(directory=DEFAULT_MEDIA_DIR):
    """Return ``(entries, truncated)`` for allowlisted ``timelapse_*.jpg`` files.

    Sorted by name, matching :func:`timelapse.list_frames` and the firmware's
    capture-order enumeration (the name embeds the capture time).
    """
    entries, truncated = _scan(directory, 'frame')
    entries.sort(key=lambda e: e.name)
    return entries, truncated


def paginate(entries, page, page_size):
    """Slice ``entries`` into one bounded page document (paths never included)."""
    total = len(entries)
    pages = (total + page_size - 1) // page_size if total else 0
    start = (page - 1) * page_size
    items = [entry.as_dict() for entry in entries[start:start + page_size]]
    return {
        'page': page,
        'page_size': page_size,
        'total': total,
        'pages': pages,
        'items': items,
    }


def open_media(directory, name, expect=None):
    """Open one allowlisted media file without following symlinks.

    Returns ``(file_object, size, kind)``. ``expect`` (``'video'``/``'frame'``)
    rejects a name of the other kind. A missing file, a symlink, a directory, a
    non-regular entry, a multiply hard-linked file, an oversized file, and a name
    outside policy all raise :class:`MediaError` with a safe message and status.
    """
    kind = classify_name(name)
    if kind is None or (expect is not None and kind != expect):
        raise MediaError('not found', 404)
    path = os.path.join(directory, name)
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        raise MediaError('not found', 404)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise MediaError('not found', 404)
        limit = MAX_VIDEO_BYTES if kind == 'video' else MAX_JPEG_BYTES
        if info.st_size < 0 or info.st_size > limit:
            raise MediaError('media too large', 413)
        handle = os.fdopen(fd, 'rb')
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return handle, info.st_size, kind


def valid_jpeg_head(head):
    """True for a JPEG start-of-image prefix (``FF D8 FF``)."""
    return (
        isinstance(head, (bytes, bytearray))
        and len(head) >= 3
        and head[0] == 0xFF and head[1] == 0xD8 and head[2] == 0xFF
    )


def free_bytes(directory):
    """Free bytes on ``directory``'s filesystem, or ``None`` if unavailable."""
    try:
        stats = os.statvfs(directory)
    except OSError:
        return None
    return stats.f_bavail * stats.f_frsize


def parse_range(value, size):
    """Parse exactly one HTTP ``bytes`` range against ``size``.

    Returns ``None`` when no ``Range`` header was sent, or an inclusive
    ``(start, end)`` tuple. Raises :class:`RangeNotSatisfiable` for a malformed
    header, multiple ranges, a non-``bytes`` unit, or a range that starts past
    the end of the file. A last-byte position past the end is clamped per RFC
    7233.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise RangeNotSatisfiable()
    spec = value.strip()
    if not spec.lower().startswith('bytes='):
        raise RangeNotSatisfiable()
    ranges = spec[len('bytes='):].strip()
    if ',' in ranges or '-' not in ranges:
        raise RangeNotSatisfiable()
    first, _, last = ranges.partition('-')
    first = first.strip()
    last = last.strip()
    if not first and not last:
        raise RangeNotSatisfiable()
    if first:
        if not first.isdigit():
            raise RangeNotSatisfiable()
        start = int(first)
        if size <= 0 or start >= size:
            raise RangeNotSatisfiable()
        if last:
            if not last.isdigit():
                raise RangeNotSatisfiable()
            end = int(last)
            if end < start:
                raise RangeNotSatisfiable()
            end = min(end, size - 1)
        else:
            end = size - 1
        return (start, end)
    # Suffix range: the last N bytes.
    if not last.isdigit():
        raise RangeNotSatisfiable()
    suffix = int(last)
    if suffix <= 0 or size <= 0:
        raise RangeNotSatisfiable()
    return (max(0, size - suffix), size - 1)
