"""Bounded, redacted current-boot diagnostics (WP-UI7; AC-17).

The System view offers a read-only diagnostics view/download. This module owns
the whole collection policy so the HTTP core never builds a command from a
request value:

* **Fixed commands only.** The journal query uses a fixed unit list, a fixed
  ``journalctl`` argv, and a fixed ``systemctl --failed`` argv. No unit, path,
  line count, priority, or time range is ever taken from a browser request.
* **Bounded.** Line count, output bytes, and wall-clock timeout are hard caps.
  A timeout or a missing tool yields a bounded, secret-free reason instead of a
  partial/raising result.
* **Redacted.** Literal secret values are scrubbed first, then fixed patterns
  cover authorization/cookie headers, ``password``/``token``/``psk`` assignments,
  JWTs, and personal home paths. Raw ``/proc/<pid>/environ``, ``ps``, and
  ``systemctl status`` are deliberately never run, so no environment or command
  line is emitted.

:class:`DiagnosticsProvider` performs the blocking collection on a daemon thread
so the single admin-core worker is never stalled; the HTTP route only reads the
cached document.
"""
import logging
import re
import subprocess
import threading
import time

import admin_auth

log = logging.getLogger('pibuddycam.diagnostics')

#: The fixed appliance units whose current-boot journal is collected. Adding a
#: unit here is a code change reviewed like any other; a request cannot extend it.
FIXED_UNITS = (
    'data-ready.target',
    'pibuddycam-boot-mode.service',
    'pibuddycam-provisioning.service',
    'NetworkManager.service',
    'pibuddycam.target',
    'rpicam-source.service',
    'pibuddycam.service',
    'pibuddycam-admin.service',
    'pibuddycam-rtsp.service',
    'pibuddycam-ha-rtsp.service',
    'pibuddycam-updater.service',
    'pibuddycam-updater-install.service',
)

#: Hard caps for one collection.
MAX_JOURNAL_LINES = 400
MAX_TEXT_BYTES = 64 * 1024
JOURNAL_TIMEOUT_SECONDS = 8.0
FAILED_UNITS_TIMEOUT_SECONDS = 4.0
MAX_REASON_CHARS = 200

#: Fixed pattern-based redaction. Order matters: header forms first, then
#: key/value secrets, then JWTs, then personal paths.
_REDACT_PATTERNS = (
    (re.compile(r'(?im)^(\s*(?:authorization|proxy-authorization)\s*[:=]\s*).*$'),
     r'\1<redacted>'),
    (re.compile(r'(?im)^(\s*(?:cookie|set-cookie)\s*[:=]\s*).*$'),
     r'\1<redacted>'),
    (re.compile(
        r'(?i)\b([a-z0-9_-]*(?:password|passwd|psk|passphrase|token|secret|'
        r'api[_-]?key|client[_-]?secret))\b(\s*[:=]\s*)'
        r'(?:"[^"]*"|\'[^\']*\'|\S+)'),
     r'\1\2<redacted>'),
    (re.compile(r'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'),
     '<redacted-jwt>'),
    (re.compile(r'(?i)(?:/home/|/users/|/root/)[^\s\'"]*'), '<path>'),
    (re.compile(r'(?i)(wifi[-_ ]?(?:ssid|psk|passphrase)\s*[:=]\s*)(\S+)'),
     r'\1<redacted>'),
    # NetworkManager-style SSID/PSK mentions (``SSID: HomeNet``, ``ssid='x'``).
    (re.compile(
        r'(?i)\b(ssid|psk|passphrase)\b(\s*[:=]\s*)'
        r'(?:"[^"]*"|\'[^\']*\'|\S+)'),
     r'\1\2<redacted>'),
)

#: Fixed, path-free reason when a command cannot be run.
_COLLECT_FAILED = 'diagnostics unavailable'


def _bounded(value, limit=MAX_REASON_CHARS):
    """Return a printable, bounded string (never raises)."""
    if isinstance(value, BaseException):
        value = str(value)
    if not isinstance(value, str):
        return ''
    cleaned = ''.join(ch for ch in value if ch.isprintable())
    return cleaned.strip()[:limit]


def _default_runner(args, timeout):
    """Run ``args`` with a bounded timeout, capturing text stdout/stderr.

    The single place this module touches :mod:`subprocess`; tests replace it.
    """
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _stdout(result):
    value = getattr(result, 'stdout', '') or ''
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value if isinstance(value, str) else ''


def _run_fixed(runner, args, timeout):
    """Run one fixed command; return ``(ok, text)`` and never raise."""
    try:
        result = runner(list(args), timeout)
    except subprocess.TimeoutExpired:
        return False, ''
    except OSError:
        return False, ''
    except Exception as e:  # noqa: BLE001 - diagnostics must never crash a request
        log.warning('diagnostics: runner failed: %s', type(e).__name__)
        return False, ''
    if getattr(result, 'returncode', 1) != 0:
        return False, ''
    return True, _stdout(result)


def redact_diagnostics(text, secrets=()):
    """Return ``text`` with literal secrets and fixed patterns removed."""
    if not isinstance(text, str) or not text:
        return ''
    safe = admin_auth.redact(text, tuple(secrets or ()))
    for pattern, replacement in _REDACT_PATTERNS:
        safe = pattern.sub(replacement, safe)
    return safe


def _bound_bytes(text, limit=MAX_TEXT_BYTES):
    """Truncate ``text`` to at most ``limit`` UTF-8 bytes without splitting."""
    if not isinstance(text, str):
        return '', False
    encoded = text.encode('utf-8')
    if len(encoded) <= limit:
        return text, False
    truncated = encoded[:limit].decode('utf-8', 'ignore')
    return truncated + '\n[diagnostics truncated]\n', True


def collect_diagnostics(runner=None, secrets=(), clock=time.time):
    """Collect the bounded, redacted current-boot diagnostics document.

    Returns a dict ``{ok, generated_at, text, lines, bytes, truncated}``. A
    command failure is reported as a bounded reason inside the text, never as a
    raised exception. Never accepts any request-derived argument.
    """
    runner = runner or _default_runner
    generated_at = clock()

    failed_ok, failed_text = _run_fixed(
        runner, ['systemctl', '--failed', '--no-pager'],
        FAILED_UNITS_TIMEOUT_SECONDS)

    journal_args = [
        'journalctl', '-b', '--no-pager', '-o', 'short-iso',
        '-n', str(MAX_JOURNAL_LINES),
    ]
    for unit in FIXED_UNITS:
        journal_args.extend(['-u', unit])
    journal_ok, journal_text = _run_fixed(
        runner, journal_args, JOURNAL_TIMEOUT_SECONDS)

    sections = [
        f'=== PiBuddyCam diagnostics (generated {time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(generated_at))}Z) ===',
        '--- failed units ---',
        failed_text.strip() if failed_ok else '(failed-unit query unavailable)',
        '--- current-boot journal (fixed units) ---',
        journal_text.strip() if journal_ok else '(journal query unavailable)',
    ]
    text = '\n'.join(sections) + '\n'
    text = redact_diagnostics(text, secrets)
    text, truncated = _bound_bytes(text)
    return {
        'ok': True,
        'generated_at': generated_at,
        'text': text,
        'lines': text.count('\n'),
        'bytes': len(text.encode('utf-8')),
        'truncated': truncated,
    }


class DiagnosticsProvider:
    """Cache one diagnostics document, refreshed on demand off the event loop.

    ``__call__`` returns the cached document immediately; when the cache is
    missing or older than ``max_age`` it starts one daemon refresh and returns a
    bounded ``collecting`` document until the next call. ``refresh`` is the
    synchronous path used by tests and by a caller that already runs off the
    core worker.
    """

    def __init__(self, runner=None, *, clock=time.time, monotonic=time.monotonic,
                 max_age=30.0):
        self._runner = runner
        self._clock = clock
        self._monotonic = monotonic
        self._max_age = max(0.0, float(max_age))
        self._lock = threading.Lock()
        self._document = None
        self._received_at = None
        self._thread = None
        self._stopped = False

    def refresh(self, secrets=()):
        """Collect one document synchronously and cache it; never raises."""
        try:
            document = collect_diagnostics(
                runner=self._runner, secrets=secrets, clock=self._clock)
        except Exception as e:  # noqa: BLE001 - a collector must never raise
            log.warning('diagnostics: collection failed: %s', type(e).__name__)
            document = {
                'ok': False,
                'generated_at': self._clock(),
                'text': '',
                'lines': 0,
                'bytes': 0,
                'truncated': False,
                'reason': _COLLECT_FAILED,
            }
        with self._lock:
            self._document = document
            self._received_at = self._monotonic()
        return document

    def snapshot(self):
        """Return the cached document (or a bounded ``collecting`` placeholder)."""
        with self._lock:
            document = self._document
            received = self._received_at
        if document is None:
            return {
                'ok': True,
                'available': False,
                'state': 'collecting',
                'generated_at': None,
                'text': '',
                'lines': 0,
                'bytes': 0,
                'truncated': False,
            }
        age = max(0.0, self._monotonic() - received) if received is not None else None
        result = dict(document)
        result['available'] = True
        result['age_seconds'] = round(age, 3) if age is not None else None
        result['fresh'] = age is not None and age <= self._max_age
        return result

    def __call__(self, secrets=()):
        """Return a snapshot, starting one background refresh when stale."""
        with self._lock:
            document = self._document
            received = self._received_at
            running = self._thread is not None and self._thread.is_alive()
            self._stopped = False
        stale = (
            document is None or received is None
            or (self._monotonic() - received) > self._max_age
        )
        if stale and not running:
            thread = threading.Thread(
                target=self.refresh, args=(tuple(secrets or ()),),
                name='diagnostics-refresh', daemon=True)
            with self._lock:
                if self._thread is None or not self._thread.is_alive():
                    self._thread = thread
                    thread.start()
        return self.snapshot()

    def stop(self):
        """Best-effort teardown; never raises."""
        with self._lock:
            self._stopped = True
        thread = self._thread
        if thread is not None:
            try:
                thread.join(timeout=1.0)
            except Exception:  # noqa: BLE001
                pass


__all__ = [
    'DiagnosticsProvider',
    'FIXED_UNITS',
    'JOURNAL_TIMEOUT_SECONDS',
    'MAX_JOURNAL_LINES',
    'MAX_TEXT_BYTES',
    'collect_diagnostics',
    'redact_diagnostics',
]
