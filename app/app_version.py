"""Application version resolution for the appliance (WP-R2; AC-24).

The version reported in the retained MQTT state/discovery documents (and later
surfaced in the web UI) is resolved from, in order:

1. the optional ``version`` field of the image's ``build-info.json``
   (``/usr/share/pibuddycam/build-info.json``),
2. the ``PIBUDDYCAM_APP_VERSION`` environment variable,
3. the development default ``0.0.0+dev``.

The build-info file is written by ``image/assets/build-info.py``. This module
never invents a release process (WP-R5 owns versioning): it only reads an
optional field and falls back. Resolution is bounded (the file is read with a
size cap) and sanitized (control characters dropped, length capped), and it
never raises.

Stdlib only, and no file/network/thread side effects on import.
"""
import json
import os

#: Default development version when neither source supplies one.
DEFAULT_VERSION = '0.0.0+dev'

#: Environment override, used when build-info.json has no ``version`` field.
ENV_VAR = 'PIBUDDYCAM_APP_VERSION'

#: In-image build-info document.
BUILD_INFO_PATH = '/usr/share/pibuddycam/build-info.json'

#: Bound the build-info read so a corrupt/huge file cannot stall startup.
MAX_BUILD_INFO_BYTES = 64 * 1024

#: Active signed application release metadata. ``make-app-release.sh`` writes
#: this file into every bundle, and the launcher runs the active release from
#: ``releases/current``, so its identity is the running application's identity
#: -- not the immutable factory image's build-info. This is what keeps the
#: dashboard from reporting the image version (``0.0.0+local``) after an OTA
#: application update.
RELEASE_METADATA_PATH = '/data/pibuddycam/releases/current/release.json'

#: Root-written HA update state; its ``installed_version`` is the fallback when
#: a release predates the bundled metadata file.
RELEASE_STATE_PATH = '/data/pibuddycam/update-state.json'

#: Bound the release/update-state reads so a corrupt file cannot stall startup.
MAX_RELEASE_METADATA_BYTES = 64 * 1024

#: Fields projected from the bundled release metadata (all non-secret).
RELEASE_METADATA_FIELDS = ('version', 'source_commit', 'channel', 'release_date')

#: Bound the sanitized version string.
MAX_VERSION_LENGTH = 128

#: Marks "use the module default", looked up at call time so tests can point the
#: module paths at a temporary tree instead of the real ``/data``.
_UNSET = object()


def _sanitize(value):
    """Return a bounded, printable version string (or ``''``)."""
    if not isinstance(value, str):
        return ''
    cleaned = ''.join(ch for ch in value if ch.isprintable())
    return cleaned.strip()[:MAX_VERSION_LENGTH]


def _build_info_document(path):
    """Read ``path`` as a JSON object; ``{}`` on any failure."""
    if not isinstance(path, str) or not path:
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read(MAX_BUILD_INFO_BYTES)
    except OSError:
        return {}
    try:
        doc = json.loads(text)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _build_info_version(path):
    """Read the optional ``version`` field from ``path``; ``''`` on any failure."""
    return _sanitize(_build_info_document(path).get('version'))


def _read_json_document(path, max_bytes=MAX_RELEASE_METADATA_BYTES):
    """Read ``path`` as a bounded JSON object; ``{}`` on any failure."""
    if not isinstance(path, str) or not path:
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read(max_bytes)
    except OSError:
        return {}
    try:
        doc = json.loads(text)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def active_release_metadata(path=_UNSET):
    """Return the bounded, non-secret identity of the active application release.

    Projects the bundled ``release.json`` onto :data:`RELEASE_METADATA_FIELDS`;
    every value is sanitized (printable, bounded) and absent/unknown fields
    become ``''``. A missing, unreadable, or malformed file yields an empty
    mapping rather than raising, so the caller can fall through to the image
    build-info. Never raises.
    """
    if path is _UNSET:
        path = RELEASE_METADATA_PATH
    doc = _read_json_document(path)
    return {
        field: _sanitize(doc.get(field))[:MAX_IDENTITY_LENGTH]
        for field in RELEASE_METADATA_FIELDS
    }


def installed_release_version(path=_UNSET):
    """Return the root updater's ``installed_version``, or ``''``.

    The update state is the fallback identity for a release installed before
    bundles carried ``release.json``. Bounded and never raises.
    """
    if path is _UNSET:
        path = RELEASE_STATE_PATH
    doc = _read_json_document(path)
    return _sanitize(doc.get('installed_version'))[:MAX_VERSION_LENGTH]


#: Build-identity fields exposed to the dashboard. Only non-secret, non-path
#: metadata is projected; ``package_manifest`` (a filesystem path) is excluded.
BUILD_IDENTITY_FIELDS = (
    'version',
    'source_commit',
    'os_suite',
    'kernel_package',
    'python_lock_sha256',
)

#: Bound for the projected build-identity string values.
MAX_IDENTITY_LENGTH = 128


def build_identity(build_info_path=_UNSET,
                   release_metadata_path=_UNSET,
                   release_state_path=_UNSET):
    """Return the bounded, non-secret build identity for the dashboard.

    The active signed release is authoritative when present: its
    ``release.json`` supplies ``version``/``source_commit`` and overrides the
    immutable factory image's ``build-info.json`` (which is why the dashboard
    no longer reports ``0.0.0+local``/an old commit after an application
    update). When a release predates the bundled metadata, the updater's
    ``installed_version`` supplies the version. Every value is sanitized
    (printable, bounded); the document paths and any unlisted key are never
    returned. Never raises.
    """
    if build_info_path is _UNSET:
        build_info_path = BUILD_INFO_PATH
    doc = _build_info_document(build_info_path)
    identity = {}
    for field in BUILD_IDENTITY_FIELDS:
        identity[field] = _sanitize(doc.get(field))[:MAX_IDENTITY_LENGTH]

    metadata = active_release_metadata(release_metadata_path)
    if metadata.get('version'):
        identity['version'] = metadata['version']
    if metadata.get('source_commit'):
        identity['source_commit'] = metadata['source_commit']
    if not metadata.get('version'):
        installed = installed_release_version(release_state_path)
        if installed:
            identity['version'] = installed
    return identity


def application_version(build_info_path=_UNSET, env=None,
                        release_metadata_path=_UNSET,
                        release_state_path=_UNSET):
    """Resolve the application version; never raises.

    Precedence: the active signed release (``release.json``), then the updater's
    recorded ``installed_version``, then the factory image ``build-info.json``,
    then the ``PIBUDDYCAM_APP_VERSION`` environment variable, then
    :data:`DEFAULT_VERSION`. The release paths and ``env`` are injectable so the
    precedence is host-testable.
    """
    if build_info_path is _UNSET:
        build_info_path = BUILD_INFO_PATH
    metadata = active_release_metadata(release_metadata_path)
    version = metadata.get('version') or installed_release_version(release_state_path)
    if version:
        return version
    version = _build_info_version(build_info_path)
    if version:
        return version
    environment = os.environ if env is None else env
    try:
        value = environment.get(ENV_VAR)
    except Exception:  # noqa: BLE001 - a broken env mapping must not raise
        value = None
    version = _sanitize(value)
    if version:
        return version
    return DEFAULT_VERSION


__all__ = [
    'BUILD_IDENTITY_FIELDS',
    'DEFAULT_VERSION',
    'ENV_VAR',
    'BUILD_INFO_PATH',
    'MAX_BUILD_INFO_BYTES',
    'MAX_IDENTITY_LENGTH',
    'MAX_RELEASE_METADATA_BYTES',
    'MAX_VERSION_LENGTH',
    'RELEASE_METADATA_FIELDS',
    'RELEASE_METADATA_PATH',
    'RELEASE_STATE_PATH',
    'active_release_metadata',
    'application_version',
    'build_identity',
    'installed_release_version',
]
