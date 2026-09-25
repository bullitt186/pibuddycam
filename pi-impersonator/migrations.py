"""Boot-time ROOT migrations for OTA-deployed fixes (runs as root in pi-persist).

Each migration is a named, idempotent function that returns ``True`` on success.
Completed migrations are tracked in ``/data/prusa-cam/migrations.json`` so they
run exactly once.  ROOT is remounted read-write only when pending migrations
exist, and remounted read-only afterward — even on failure.

Adding a migration
------------------
1. Write a function ``_nnn_description(root)`` that applies the fix idempotently.
   ``root`` is ``'/'`` on the device and an injectable test path otherwise.
   Return ``True`` on success (including already-applied); ``False`` on failure.
2. Append ``('nnn_description', _nnn_description)`` to :data:`MIGRATIONS`.
3. The name is the durable key — never rename a shipped migration.

The image build (``install-factory-app.sh``) should still carry the same fix so
a fresh image doesn't need the migration.  Migrations are the catch-up path for
devices running an older image with a newer OTA release.
"""
import json
import logging
import os
import subprocess

log = logging.getLogger('prusa-cam.migrations')

MIGRATION_STATE_PATH = '/data/prusa-cam/migrations.json'

# -- migration functions ---------------------------------------------------- #


def _001_create_mnt_sdcard(root):
    """Create /mnt/sdcard so pi-persist can bind-mount /data/sdcard onto it."""
    target = os.path.join(root, 'mnt', 'sdcard')
    os.makedirs(target, exist_ok=True)
    return os.path.isdir(target)


def _002_install_samba_config(root):
    """Write the [sdcard] SMB share config and volatile-state tmpfiles."""
    samba_dir = os.path.join(root, 'etc', 'samba')
    share_conf = os.path.join(samba_dir, 'smb-sdcard.conf')
    main_conf = os.path.join(samba_dir, 'smb.conf')
    tmpfiles = os.path.join(root, 'etc', 'tmpfiles.d', 'buddy3d-samba.conf')

    os.makedirs(samba_dir, exist_ok=True)
    with open(share_conf, 'w') as f:
        f.write(
            '[sdcard]\n'
            '   path = /mnt/sdcard\n'
            '   browseable = yes\n'
            '   read only = no\n'
            '   guest ok = yes\n'
            '   force user = prusa-cam\n'
            '   create mask = 0644\n'
            '   directory mask = 0755\n'
        )

    include_line = 'include = /etc/samba/smb-sdcard.conf'
    if os.path.isfile(main_conf):
        with open(main_conf) as f:
            content = f.read()
        if include_line not in content:
            with open(main_conf, 'a') as f:
                f.write(f'\n{include_line}\n')
    else:
        with open(main_conf, 'w') as f:
            f.write(f'[global]\n   workgroup = WORKGROUP\n\n{include_line}\n')

    os.makedirs(os.path.dirname(tmpfiles), exist_ok=True)
    with open(tmpfiles, 'w') as f:
        f.write(
            '# Samba volatile state on the tmpfs /var (buddy3d appliance).\n'
            'd /var/lib/samba          0755 root root -\n'
            'd /var/lib/samba/private  0700 root root -\n'
            'd /var/log/samba          0755 root root -\n'
            'd /var/cache/samba        0755 root root -\n'
        )
    return True


def _003_mask_console_setup(root):
    """Mask console-setup.service (headless appliance, ro ROOT)."""
    unit = os.path.join(root, 'etc', 'systemd', 'system', 'console-setup.service')
    if os.path.islink(unit) and os.readlink(unit) == '/dev/null':
        return True
    if os.path.exists(unit) and not os.path.islink(unit):
        os.remove(unit)
    os.makedirs(os.path.dirname(unit), exist_ok=True)
    os.symlink('/dev/null', unit)
    return True


def _004_prusa_priv_no_block(root):
    """Change start-camera in prusa-priv to use --no-block."""
    helper = os.path.join(root, 'usr', 'libexec', 'prusa-cam', 'prusa-priv')
    if not os.path.isfile(helper):
        log.warning('migrations: prusa-priv not found at %s', helper)
        return False
    with open(helper) as f:
        content = f.read()
    old = 'exec "$SYSTEMCTL" start prusa-camera.target'
    new = 'exec "$SYSTEMCTL" --no-block start prusa-camera.target'
    if new in content:
        return True
    if old not in content:
        log.warning('migrations: prusa-priv start-camera line not found')
        return False
    content = content.replace(old, new)
    with open(helper, 'w') as f:
        f.write(content)
    return True


# -- registry (append only; never reorder or rename shipped entries) -------- #

MIGRATIONS = [
    ('001_create_mnt_sdcard', _001_create_mnt_sdcard),
    ('002_install_samba_config', _002_install_samba_config),
    ('003_mask_console_setup', _003_mask_console_setup),
    ('004_prusa_priv_no_block', _004_prusa_priv_no_block),
]


# -- runner ----------------------------------------------------------------- #


def _load_state(path=MIGRATION_STATE_PATH):
    try:
        with open(path) as f:
            data = json.load(f)
        return set(data.get('applied', []))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()


def _save_state(applied, path=MIGRATION_STATE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'applied': sorted(applied)}, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def _remount(mode):
    result = subprocess.run(
        ['mount', '-o', f'remount,{mode}', '/'],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.warning('migrations: remount %s failed: %s', mode, result.stderr.strip())
        return False
    return True


def run_pending(root='/', state_path=MIGRATION_STATE_PATH):
    """Apply any migrations not yet recorded in the state file.

    Returns the list of newly applied migration names.  ROOT is only
    remounted when there is at least one pending migration.
    """
    applied = _load_state(state_path)
    pending = [(name, fn) for name, fn in MIGRATIONS if name not in applied]
    if not pending:
        return []

    log.info('migrations: %d pending: %s',
             len(pending), ', '.join(n for n, _ in pending))

    remounted = _remount('rw')
    if not remounted:
        log.warning('migrations: could not remount ROOT rw; skipping ROOT migrations')

    newly_applied = []
    for name, fn in pending:
        try:
            ok = fn(root)
        except Exception as e:
            log.warning('migrations: %s failed: %s', name, e)
            ok = False
        if ok:
            applied.add(name)
            newly_applied.append(name)
            log.info('migrations: applied %s', name)
        else:
            log.warning('migrations: %s did not complete', name)

    if remounted:
        _remount('ro')

    _save_state(applied, state_path)
    return newly_applied
