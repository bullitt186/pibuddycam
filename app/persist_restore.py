"""One-shot restore of durable settings + timelapse store (GAP-PERSIST-01).

Runs as root from ``pi-persist.service`` before the camera/RTSP units. It:

1. bails out (exit 0) unless ``/data`` is a real mountpoint, so it is harmless
   before the offline repartition creates ``mmcblk0p3``;
2. creates the durable directories and bind-mounts ``/data/sdcard`` onto the
   firmware path ``/mnt/sdcard`` (SMB keeps sharing ``/mnt/sdcard`` unchanged),
   and ``/data/network/system-connections`` onto
   ``/etc/NetworkManager/system-connections`` so the station profile created at
   claim survives the read-only-root reboot (B4);
3. re-applies the configured hostname (``device.toml [admin].hostname``) so the
   certificate SAN and the mDNS name match it, then provisions the durable admin self-signed keypair and recreates the volatile
   ``/etc/pibuddycam/admin.env`` so ``pibuddycam-admin.service`` can serve HTTPS
   (appliance image/security defect; see :mod:`admin_tls`);
4. restores ``quality.env``, ``rotation.env`` and ``rtsp.mode`` from
   ``state.json``;
5. keeps the last known clock across reboots (there is no RTC): the timesyncd
   state directory is bind-mounted from ``/data`` and timesyncd is restarted, and
   the NTP drop-in is regenerated from ``device.toml [network] ntp_servers``;
6. prunes the oldest timelapse JPEG frames, including the ones inside per-print
   ``session_*`` folders, when ``/data`` free space is low (``.avi`` and the CSV
   index are never deleted).

The top level is side-effect free: importing this module must not touch the
filesystem, so the work lives in :func:`main` behind the ``__main__`` guard.
Stdlib only.
"""
import logging
import os
import shutil
import stat
import subprocess
import sys

import admin_tls
import config_schema
import migrations
import network_settings
import ntp_apply
import quality
import rotation
import rtsp_control
import settings_store
import timelapse

log = logging.getLogger('pibuddycam.persist')

DATA_MOUNT = '/data'
DATA_SDCARD = '/data/sdcard'
DATA_PIBUDDYCAM = '/data/pibuddycam'
DATA_CONFIG_DIR = DATA_PIBUDDYCAM + '/config'
DATA_RELEASES_DIR = DATA_PIBUDDYCAM + '/releases'
DATA_BACKUPS_DIR = DATA_PIBUDDYCAM + '/backups'
DATA_NETWORK_DIR = '/data/network'
DATA_NETWORK_CONNECTIONS = DATA_NETWORK_DIR + '/system-connections'
SD_MOUNT = '/mnt/sdcard'
#: NetworkManager keyfile store. Bind-mounted from DATA_NETWORK_CONNECTIONS so
#: the station profile created at claim survives the read-only-root reboot (B4).
NM_CONNECTIONS = '/etc/NetworkManager/system-connections'
#: timesyncd's clock file lives here; /var is tmpfs, so without a durable mount the
#: clock falls back to the image build time at every boot (no RTC).
DATA_TIMESYNC = DATA_PIBUDDYCAM + '/timesync'
TIMESYNC_STATE = '/var/lib/systemd/timesync'
TIMELAPSE_DIR = DATA_SDCARD + '/timelapse'
FRAME_SUFFIX = '.jpg'

# Free-space floor below which stored frames are pruned (300 MB).
PRUNE_FREE_THRESHOLD_BYTES = 300 * 1024 * 1024

# Dedicated non-login service account that owns every durable directory. The
# account is created by the image installer and is the single identity in the
# systemd units; override only for a non-standard install via SERVICE_USER.
DEFAULT_SERVICE_USER = 'pibuddycam'

# Durable directory layout on the PERSIST partition, in creation order, with the
# mode each directory must end up with. ``config``/``backups`` hold secrets and
# migration backups, so they are group-accessible but not world-readable; the
# media directories stay 0755 so the bind-mounted SMB share can traverse them.
# ``releases`` is world-traversable (0755) but root-owned (see ROOT_ONLY_DIRS):
# only the root updater may write installation targets.
DATA_LAYOUT = (
    (DATA_SDCARD, 0o755),
    (TIMELAPSE_DIR, 0o755),
    (DATA_PIBUDDYCAM, 0o750),
    (DATA_CONFIG_DIR, 0o750),
    (DATA_RELEASES_DIR, 0o755),
    (DATA_BACKUPS_DIR, 0o750),
    (DATA_NETWORK_DIR, 0o700),
    (DATA_NETWORK_CONNECTIONS, 0o700),
    (DATA_TIMESYNC, 0o755),
)

#: Layout entries that must stay root-owned. The NetworkManager keyfile store is
#: bind-mounted onto ``/etc/NetworkManager/system-connections`` (see
#: :data:`NM_CONNECTIONS`), which root consumes and inotify-reloads; giving the
#: unprivileged service account write access there would both weaken the
#: privilege boundary and let NM reject the profiles. ``releases`` holds the
#: signed application releases the root updater installs and swaps; if the
#: service account owned it, it could tamper with (or replace) installation
#: targets. These entries are created (and, to repair an older image seed,
#: explicitly re-asserted) as root by ``pi-persist.service``; they are never
#: handed to the service user.
ROOT_ONLY_DIRS = frozenset({
    DATA_TIMESYNC,
    DATA_NETWORK_DIR,
    DATA_NETWORK_CONNECTIONS,
    DATA_RELEASES_DIR,
})


def quality_env_values(tier):
    """Return the ``(width, height)`` a persisted quality tier maps to.

    Pure mirror of ``quality.write_current``'s resolution table so the restore
    decision is testable without writing ``/etc/pibuddycam``.
    """
    resolutions = quality.RESOLUTIONS
    return resolutions.get(tier, resolutions[quality.DEFAULT_QUALITY])


def frames_to_prune(frames, total_bytes, limit_bytes):
    """Select the oldest frames to delete to bring stored bytes under a limit.

    ``frames`` is an iterable of ``(name, size, mtime)`` for prunable JPEG
    frames. Selection is oldest-first (ascending ``mtime``, filename as a
    tie-break). Returns ``[(name, size), ...]``; empty when ``total_bytes`` is
    already at or below ``limit_bytes``. Pure: performs no I/O.
    """
    if total_bytes <= limit_bytes:
        return []
    ordered = sorted(frames, key=lambda frame: (frame[2], frame[0]))
    freed = 0
    selected = []
    for name, size, _mtime in ordered:
        if total_bytes - freed <= limit_bytes:
            break
        selected.append((name, size))
        freed += size
    return selected


def _chown(path, user):
    """Best-effort ``chown`` of ``path`` to ``user`` (never raises)."""
    try:
        shutil.chown(path, user=user)
    except (LookupError, OSError, KeyError) as e:
        log.warning(f'persist: could not chown {path} to {user}: {e}')


def _bind_mount(source, target):
    """Bind-mount ``source`` onto ``target`` unless already mounted there."""
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        log.warning(f'persist: could not create {target}: {e}')
        return False
    if os.path.ismount(target):
        log.info(f'persist: {target} already mounted; leaving as-is')
        return True
    try:
        result = subprocess.run(
            ['mount', '--bind', source, target], capture_output=True, text=True
        )
    except OSError as e:
        log.warning(f'persist: bind mount {source} -> {target} failed: {e}')
        return False
    if result.returncode != 0:
        log.warning(
            f'persist: bind mount {source} -> {target} failed '
            f'(rc={result.returncode}): {result.stderr.strip()}'
        )
        return False
    log.info(f'persist: bind-mounted {source} -> {target}')
    return True


def ensure_durable_layout(service_user):
    """Create, mode, and chown the durable ``/data`` layout.

    Idempotent and best-effort: a directory that cannot be created is logged and
    skipped so one bad path never blocks the settings restore. Returns the list
    of directories that exist (and were chowned) afterwards. Callers must check
    :func:`settings_store.available` first; this helper does not verify that
    ``/data`` is a real mountpoint.
    """
    created = []
    for directory, mode in DATA_LAYOUT:
        try:
            os.makedirs(directory, exist_ok=True)
            os.chmod(directory, mode)
        except OSError as e:
            log.warning(f'persist: could not create {directory}: {e}')
            continue
        # Root-only entries stay root-owned (see ROOT_ONLY_DIRS) and are
        # explicitly re-asserted so an image seed or an older layout that handed
        # them to the service account is repaired. pi-persist.service runs as
        # root, so the chown succeeds.
        if directory in ROOT_ONLY_DIRS:
            _chown(directory, 'root')
        else:
            _chown(directory, service_user)
        created.append(directory)
    return created


def _provision_admin_tls(service_user=DEFAULT_SERVICE_USER):
    """Provision the durable admin keypair + volatile ``admin.env`` (best-effort).

    Failure is isolated: the camera/RTSP stack must still come up, and the admin
    service fails closed on its own when ``admin.env`` is absent or broken. The
    warning is the operator's only signal, so it names the module and reason.
    """
    try:
        admin_tls.ensure(service_user)
        log.info('persist: admin TLS keypair + admin.env provisioned')
    except admin_tls.TlsError as e:
        log.warning(f'persist: admin TLS provisioning failed: {e}')
    except Exception as e:  # noqa: BLE001 - never block the settings restore
        log.warning(f'persist: admin TLS provisioning failed unexpectedly: {e}')


def _apply_hostname(device_path=None, runner=subprocess.run):
    """Re-apply ``device.toml [admin].hostname`` as the transient hostname.

    The root filesystem is read-only, so the console's hostname change is stored
    in ``device.toml`` and re-applied here at every boot, before the admin
    certificate is provisioned (it is regenerated when the names change). An
    unset or invalid hostname leaves the image default. Best-effort, never raises.
    """
    try:
        device = (
            config_schema.load_device(device_path)
            if device_path is not None else config_schema.load_device()
        )
        raw = (device.get('admin') or {}).get('hostname', '')
        name = network_settings.validate_hostname(raw) if raw else ''
    except (config_schema.ConfigError, network_settings.ValidationError):
        return False
    except Exception as e:  # noqa: BLE001 - never block the settings restore
        log.warning(f'persist: could not read the hostname: {type(e).__name__}')
        return False
    if not name:
        return False
    try:
        result = runner(
            ['hostnamectl', 'set-hostname', '--transient', name],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning(f'persist: hostnamectl failed: {type(e).__name__}')
        return False
    if result.returncode != 0:
        log.warning(f'persist: hostnamectl exited {result.returncode}')
        return False
    log.info(f'persist: hostname applied ({name})')
    return True


def _restore_time_sync(runner=subprocess.run):
    """Persist timesyncd's clock and apply the NTP servers (best-effort).

    The state directory may be a symlink into ``/var/lib/private`` (DynamicUser),
    so the bind target is its resolved path. **[assumption]** verify on hardware
    that timesyncd keeps the clock file there. The drop-in is written first and
    timesyncd is restarted once, so it starts with both the durable clock and the
    configured servers.
    """
    target = os.path.realpath(TIMESYNC_STATE)
    mounted = _bind_mount(DATA_TIMESYNC, target)
    try:
        ntp_apply.apply(restart=lambda: None)
    except Exception as e:  # noqa: BLE001 - never block the settings restore
        log.warning(f'persist: NTP drop-in failed: {type(e).__name__}')
    try:
        runner(['systemctl', 'try-restart', ntp_apply.UNIT],
               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning(f'persist: could not restart timesyncd: {type(e).__name__}')
    return mounted


def _restore_settings(service_user=DEFAULT_SERVICE_USER):
    """Materialize quality.env, rotation.env and rtsp.mode from state.json.

    pi-persist runs as root, so the files it creates are root-owned. The
    service account must be able to overwrite them at runtime, so each
    restored file is chowned to the service user after writing.
    """
    data = settings_store.load()
    if not data:
        log.info('persist: no persisted settings to restore')
        return
    tier = data.get('quality_tier')
    if type(tier) is int and tier in quality.RESOLUTIONS:
        try:
            quality.write_current(tier)
            _chown(quality.QUALITY_ENV, service_user)
            log.info(f'persist: restored quality tier {tier} to {quality.QUALITY_ENV}')
        except OSError as e:
            log.warning(f'persist: could not restore quality tier {tier}: {e}')
    degrees = data.get('rotation')
    if rotation.valid_rotation(degrees) is not None:
        try:
            rotation.write_current(degrees)
            _chown(rotation.ROTATION_ENV, service_user)
            log.info(f'persist: restored rotation {degrees} to {rotation.ROTATION_ENV}')
        except OSError as e:
            log.warning(f'persist: could not restore rotation {degrees}: {e}')
    mode = data.get('rtsp_mode')
    if mode in (rtsp_control.RTSP_DISABLED, rtsp_control.RTSP_ENABLED):
        if rtsp_control.write_mode(mode):
            _chown(rtsp_control.RTSP_MODE_FILE, service_user)
            log.info(f'persist: restored rtsp mode {mode} to {rtsp_control.RTSP_MODE_FILE}')
        else:
            log.warning(f'persist: could not restore rtsp mode {mode}')


def _frame_inventory(directory):
    """Return ``(name, size, mtime)`` for regular ``.jpg`` frames in ``directory``.

    Frames inside per-print ``session_*`` folders are included with a relative
    ``session_x/frame.jpg`` name, so a session can never escape the free-space
    guard.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    frames = []
    for name in names:
        path = os.path.join(directory, name)
        if timelapse.valid_session_name(name):
            if os.path.islink(path) or not os.path.isdir(path):
                continue
            for inner_name, size, mtime in _frame_inventory_flat(path):
                frames.append((f'{name}/{inner_name}', size, mtime))
            continue
        item = _frame_stat(directory, name)
        if item is not None:
            frames.append(item)
    return frames


def _frame_stat(directory, name):
    if not name.endswith(FRAME_SUFFIX):
        return None
    try:
        st = os.lstat(os.path.join(directory, name))
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return (name, st.st_size, st.st_mtime)


def _frame_inventory_flat(directory):
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return [item for item in (_frame_stat(directory, n) for n in names) if item]


def _remove_empty_sessions(directory):
    """Remove emptied session folders (never the one named by the marker)."""
    active = timelapse.active_session(directory)
    for name in timelapse.list_sessions(directory):
        if name == active:
            continue
        try:
            os.rmdir(os.path.join(directory, name))   # only succeeds when empty
        except OSError:
            pass


def _prune_timelapse(directory=TIMELAPSE_DIR, mount=DATA_MOUNT):
    """Delete oldest JPEG frames when free space is below the threshold.

    Frames in ``session_*`` folders are pruned too (oldest first across all
    folders) and emptied session folders are removed. ``.avi`` and the
    ``.timelapse_videos.csv`` index are never touched.
    """
    try:
        free = shutil.disk_usage(mount).free
    except OSError as e:
        log.warning(f'persist: could not stat free space on {mount}: {e}')
        return
    if free >= PRUNE_FREE_THRESHOLD_BYTES:
        return
    frames = _frame_inventory(directory)
    if not frames:
        return
    total_bytes = sum(size for _name, size, _mtime in frames)
    # Bytes that must be freed so free space reaches the threshold.
    need = PRUNE_FREE_THRESHOLD_BYTES - free
    limit = max(0, total_bytes - need)
    selected = frames_to_prune(frames, total_bytes, limit)
    removed = 0
    for name, _size in selected:
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError as e:
            log.warning(f'persist: could not prune {name}: {e}')
    _remove_empty_sessions(directory)
    log.info(
        f'persist: free {free // (1024 * 1024)} MB < '
        f'{PRUNE_FREE_THRESHOLD_BYTES // (1024 * 1024)} MB; pruned {removed} frame(s)'
    )


def main():
    """Restore durable state; returns a process exit code (always 0)."""
    if not settings_store.available():
        log.warning('persist: /data is not a mountpoint; nothing to restore')
        return 0

    # OTA-deployed ROOT migrations run first — they may create mountpoints or
    # install config files that the rest of this function depends on (e.g.
    # /mnt/sdcard, samba config, pibuddycam-priv fixes).  The migration runner
    # handles rw/ro remount and idempotency tracking.
    try:
        applied = migrations.run_pending()
        if applied:
            log.info(f'persist: applied {len(applied)} migration(s): {", ".join(applied)}')
    except Exception as e:
        log.warning(f'persist: migration runner failed: {e}')

    service_user = os.environ.get('SERVICE_USER', DEFAULT_SERVICE_USER)
    ensure_durable_layout(service_user)
    _apply_hostname()
    _provision_admin_tls(service_user)

    _bind_mount(DATA_SDCARD, SD_MOUNT)
    _bind_mount(DATA_NETWORK_CONNECTIONS, NM_CONNECTIONS)
    _restore_time_sync()
    _restore_settings(service_user)
    _prune_timelapse()
    return 0


if __name__ == '__main__':
    sys.exit(main())
