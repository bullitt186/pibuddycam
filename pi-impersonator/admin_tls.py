"""Boot-time self-signed TLS provisioning for the claimed admin console.

Appliance image/security defect (not a firmware ``GAP-*`` item). The admin
transport (:mod:`admin_app`) consumes ``ADMIN_TLS_CERT``/``ADMIN_TLS_KEY`` from
``/etc/pibuddycam/admin.env``, but nothing generated them, so a claimed device
served the admin UI as **plaintext HTTP on port 443**. This module closes that
defect:

1. it generates a device self-signed certificate + private key once, durably,
   under ``/data/pibuddycam/config/admin-tls/`` and reuses the same keypair on
   every later boot (a LAN IP change never regenerates it);
2. it recreates the volatile ``/etc/pibuddycam/admin.env`` on every boot (the
   file lives on tmpfs and is lost across reboots);
3. it fails closed: when the keypair cannot be provisioned, ``admin.env`` is not
   written and :mod:`admin_app` refuses to start in ``admin`` mode, so the
   console can never fall back to plaintext.

The private key is ``0600`` and owned by the service account; the certificate is
``0644``; ``admin.env`` is ``0640``. Writes are atomic (temp + ``os.replace``) so
a symlink planted at a destination path is replaced, not followed.

``openssl`` is used to generate the certificate and to revalidate an existing
one (key/cert match, SAN coverage, remaining validity). It is invoked with an
argv list and never through a shell, so no value is subject to shell
interpolation. The image installs ``openssl`` explicitly (see
``image/layer/pibuddycam-image.yaml`` and ``image/config/pibuddycam-pi-zero2w.yaml``).

Stdlib only; importing this module performs no filesystem I/O.
"""
import logging
import os
import pwd
import re
import shutil
import ssl
import subprocess
import tempfile

import config_schema
import provisioning

log = logging.getLogger('pibuddycam.admin_tls')

#: Durable directory holding the device keypair (PERSIST partition).
TLS_DIR = '/data/pibuddycam/config/admin-tls'
TLS_CERT_PATH = TLS_DIR + '/admin.crt'
TLS_KEY_PATH = TLS_DIR + '/admin.key'

#: Volatile environment file consumed by ``pibuddycam-admin.service`` (tmpfs).
ADMIN_ENV_PATH = '/etc/pibuddycam/admin.env'

DEFAULT_SERVICE_USER = 'pibuddycam'
DEFAULT_OPENSSL = 'openssl'

CERT_MODE = 0o644
KEY_MODE = 0o600
ENV_MODE = 0o640
DIR_MODE = 0o750

#: Ten years. A self-signed appliance certificate has no CA to rotate it, so a
#: long life avoids an expiry cliff; the keypair is regenerated only if it is
#: missing, corrupt, expiring soon, or missing the canonical hostname in its SAN.
VALIDITY_DAYS = 3650

#: Regenerate when the certificate expires within this window (30 days).
RENEW_WITHIN_SECONDS = 30 * 24 * 3600

_LABEL_RE = re.compile(r'[^a-z0-9-]+')


class TlsError(RuntimeError):
    """Provisioning failed; the admin console must not start."""


class TlsConfigurationError(RuntimeError):
    """Admin mode has no usable TLS configuration; refuse plaintext startup."""


def _sanitize_label(value):
    """Project ``value`` onto a DNS-label-safe lowercase ``[a-z0-9-]`` token."""
    if not isinstance(value, str):
        return ''
    return _LABEL_RE.sub('-', value.strip().lower()).strip('-')[:63]


def hostname_candidates(device_path=None):
    """Return the distinct DNS names the certificate must cover.

    Prefers the configured ``device.toml [admin].hostname`` (what the wizard
    persists at claim) and also includes the derived ``pibuddycam-<last6>`` name, so
    a certificate provisioned before claim still matches the canonical
    ``pibuddycam-<device-id>.local`` address afterwards. Both are sanitized to a
    valid DNS label; the result may be empty when no identity is available.
    """
    configured = ''
    try:
        device = (
            config_schema.load_device(device_path)
            if device_path is not None else config_schema.load_device()
        )
        if isinstance(device, dict):
            admin = device.get('admin')
            if isinstance(admin, dict):
                configured = admin.get('hostname', '')
    except Exception:  # noqa: BLE001 - a corrupt document must not stop provisioning
        configured = ''

    derived = ''
    try:
        derived = provisioning.admin_hostname(
            provisioning.resolve_device_id(device_path))
    except Exception:  # noqa: BLE001 - identity resolution must never raise
        derived = ''

    names = []
    for raw in (configured, derived):
        name = _sanitize_label(raw)
        if name and name not in names:
            names.append(name)
    return names


def _san_entries(candidates):
    """Build the ``subjectAltName`` entries for ``candidates``.

    Each candidate is covered both bare and as ``<name>.local``; ``localhost``
    and the loopback IP are always included so the console works on the device
    itself. LAN-IP access keeps a browser name warning by design.
    """
    dns = []
    for name in candidates:
        for value in (name, name + '.local'):
            if value not in dns:
                dns.append(value)
    if 'localhost' not in dns:
        dns.append('localhost')
    return ['DNS:' + value for value in dns] + ['IP:127.0.0.1']


def _common_name(candidates):
    if candidates:
        return candidates[0] + '.local'
    return 'localhost'


def _service_ids(user):
    """Return ``(uid, gid)`` for ``user``; ``(-1, -1)`` when it does not exist."""
    try:
        entry = pwd.getpwnam(user)
    except (KeyError, OSError):
        log.warning('admin_tls: service account %r not found; keypair ownership '
                    'not asserted', user)
        return -1, -1
    return entry.pw_uid, entry.pw_gid


def _default_chown(path, uid, gid):
    """Chown ``path`` when a real id is known; best-effort (never raises)."""
    if uid is None or gid is None or uid < 0 or gid < 0:
        return
    try:
        os.chown(path, uid, gid)
    except OSError as e:
        log.warning(f'admin_tls: could not chown {path}: {e}')


def _run_ok(runner, cmd):
    """True when ``cmd`` exits 0; never raises."""
    try:
        result = runner(cmd, capture_output=True, text=True)
    except OSError:
        return False
    return getattr(result, 'returncode', 1) == 0


#: SAN DNS entries as printed by ``openssl x509 -ext subjectAltName``.
_SAN_DNS_RE = re.compile(r'DNS:([A-Za-z0-9._-]+)')


def _cert_covers_host(runner, openssl, cert, host):
    """True when ``cert`` carries ``host`` in its subjectAltName.

    ``openssl x509 -checkhost`` returns 0 on OpenSSL 3.0 even for a mismatch, so
    the SAN extension is parsed explicitly instead of trusting its exit code.
    """
    try:
        result = runner(
            [openssl, 'x509', '-in', cert, '-noout', '-ext', 'subjectAltName'],
            capture_output=True, text=True,
        )
    except OSError:
        return False
    if getattr(result, 'returncode', 1) != 0:
        return False
    names = _SAN_DNS_RE.findall(getattr(result, 'stdout', '') or '')
    return host in names


def _keypair_reusable(cert, key, candidates, openssl, runner):
    """True when the existing keypair can be safely reused.

    A symlink at either path is rejected (the generator then replaces it).
    ``ssl`` proves the key matches the certificate; ``openssl x509`` proves the
    canonical hostname is in the SAN and that the certificate is not about to
    expire. Any failure means "regenerate", never "serve anyway".
    """
    if os.path.islink(cert) or os.path.islink(key):
        return False
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        return False
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
    except (OSError, ssl.SSLError):
        return False
    host = _common_name(candidates)
    if not _cert_covers_host(runner, openssl, cert, host):
        return False
    if not _run_ok(
        runner,
        [openssl, 'x509', '-in', cert, '-noout', '-checkend', str(RENEW_WITHIN_SECONDS)],
    ):
        return False
    return True


def _generate_keypair(tls_dir, cert_path, key_path, candidates, openssl, runner):
    """Generate the keypair into ``tls_dir`` via ``openssl`` (argv list, no shell).

    The certificate and key are written into a private temp directory, then moved
    into place with ``os.replace`` so a reader never sees a partial file and a
    pre-existing symlink at the destination is replaced rather than followed.
    """
    os.makedirs(tls_dir, exist_ok=True)
    os.chmod(tls_dir, DIR_MODE)
    staging = tempfile.mkdtemp(prefix='.admin-tls-', dir=tls_dir)
    key_tmp = os.path.join(staging, 'admin.key')
    cert_tmp = os.path.join(staging, 'admin.crt')
    try:
        san = ','.join(_san_entries(candidates))
        cmd = [
            openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-sha256', '-nodes',
            '-days', str(VALIDITY_DAYS),
            '-keyout', key_tmp, '-out', cert_tmp,
            '-subj', '/CN=' + _common_name(candidates),
            '-addext', 'subjectAltName=' + san,
            '-addext', 'basicConstraints=critical,CA:FALSE',
            '-addext', 'keyUsage=critical,digitalSignature,keyEncipherment',
            '-addext', 'extendedKeyUsage=serverAuth',
        ]
        try:
            result = runner(cmd, capture_output=True, text=True)
        except OSError as e:
            raise TlsError(f'openssl could not be executed: {e}')
        if getattr(result, 'returncode', 1) != 0:
            detail = (getattr(result, 'stderr', '') or '').strip()
            raise TlsError(f'openssl keypair generation failed: {detail}')
        if not (os.path.isfile(key_tmp) and os.path.isfile(cert_tmp)):
            raise TlsError('openssl reported success but wrote no keypair')
        os.chmod(key_tmp, KEY_MODE)
        os.chmod(cert_tmp, CERT_MODE)
        # Key first, certificate second: if the process dies between the two
        # renames the next boot sees a mismatched pair and regenerates.
        os.replace(key_tmp, key_path)
        os.replace(cert_tmp, cert_path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _cleanup_stale_staging(tls_dir):
    """Remove leftover ``.admin-tls-*`` staging directories from a crash."""
    try:
        names = os.listdir(tls_dir)
    except OSError:
        return
    for name in names:
        if not name.startswith('.admin-tls-'):
            continue
        path = os.path.join(tls_dir, name)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def _write_admin_env(env_path, cert, key):
    """Atomically (re)create ``admin.env`` with the TLS paths."""
    text = f'ADMIN_TLS_CERT={cert}\nADMIN_TLS_KEY={key}\n'
    try:
        config_schema.write_atomic(env_path, text, mode=ENV_MODE)
    except config_schema.ConfigError as e:
        raise TlsError(f'could not write {env_path}: {e}')


def ensure(service_user=DEFAULT_SERVICE_USER, *, tls_dir=TLS_DIR,
           env_path=ADMIN_ENV_PATH, device_path=None, hostnames=None,
           openssl=None, runner=subprocess.run, which=shutil.which,
           chown=_default_chown, service_ids=None):
    """Provision (idempotently) the durable keypair and the volatile env file.

    ``hostnames``/``openssl``/``runner``/``which``/``chown``/``service_ids`` and
    the two paths are injectable so tests never touch the real ``/data`` or
    ``/etc/pibuddycam``. Raises :class:`TlsError` on any failure; the caller
    (``pi-persist``) logs it and leaves ``admin.env`` absent, so the admin
    service fails closed.
    """
    if openssl is None:
        openssl = which(DEFAULT_OPENSSL) if which is not None else None
    if not openssl:
        raise TlsError('openssl is not installed; cannot provision admin TLS')

    if hostnames is None:
        candidates = hostname_candidates(device_path)
    else:
        candidates = []
        for raw in hostnames:
            name = _sanitize_label(raw)
            if name and name not in candidates:
                candidates.append(name)

    cert = os.path.join(tls_dir, os.path.basename(TLS_CERT_PATH))
    key = os.path.join(tls_dir, os.path.basename(TLS_KEY_PATH))
    uid, gid = service_ids if service_ids is not None else _service_ids(service_user)

    os.makedirs(tls_dir, exist_ok=True)
    os.chmod(tls_dir, DIR_MODE)
    # The service account must be able to traverse the directory even when a
    # root-run pi-persist created it. Re-assert ownership on every ensure
    # (generation and reuse) so a mis-owned directory is repaired, matching the
    # key/cert/env re-assertion below.
    chown(tls_dir, uid, gid)
    _cleanup_stale_staging(tls_dir)

    if not _keypair_reusable(cert, key, candidates, openssl, runner):
        _generate_keypair(tls_dir, cert, key, candidates, openssl, runner)

    # Re-assert the security-relevant modes/ownership even on reuse (repair).
    try:
        os.chmod(key, KEY_MODE)
        os.chmod(cert, CERT_MODE)
    except OSError as e:
        raise TlsError(f'could not set keypair modes: {e}')
    chown(key, uid, gid)
    chown(cert, uid, gid)

    _write_admin_env(env_path, cert, key)
    chown(env_path, uid, gid)
    log.info('admin_tls: provisioned admin TLS for %s', ', '.join(candidates) or 'localhost')
    return True


def context_from_env(env=None):
    """Return a server SSL context from ``ADMIN_TLS_CERT``/``ADMIN_TLS_KEY``.

    Returns ``None`` when either path is unset. An invalid/unreadable pair
    raises from :meth:`ssl.SSLContext.load_cert_chain`, which the caller lets
    propagate so admin startup fails closed.
    """
    env = os.environ if env is None else env
    cert = env.get('ADMIN_TLS_CERT')
    key = env.get('ADMIN_TLS_KEY')
    if not cert or not key:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context


def resolve_server_tls(mode, ssl_context=None, env=None, context_factory=None):
    """Resolve the server TLS context for ``mode``, failing closed in admin mode.

    * an explicitly injected ``ssl_context`` always wins (embedding/hardware
      bring-up);
    * ``setup`` mode returns ``None`` so the captive portal stays plain HTTP;
    * ``admin`` mode requires a context from ``context_factory`` (default
      :func:`context_from_env`) and raises :class:`TlsConfigurationError` when
      none is configured. A configured-but-broken certificate raises from the
      loader, which the caller also treats as fatal.
    """
    if ssl_context is not None:
        return ssl_context
    if mode != 'admin':
        return None
    factory = context_factory or context_from_env
    context = factory(env)
    if context is None:
        raise TlsConfigurationError(
            'admin mode requires ADMIN_TLS_CERT/ADMIN_TLS_KEY; '
            'refusing to serve plaintext HTTP on the admin port')
    return context
