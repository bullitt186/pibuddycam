"""Boot-time ROOT migrations for OTA-deployed fixes (runs as root in pi-persist).

Each migration is a named, idempotent function that returns ``True`` on success.
Completed migrations are tracked in ``/data/pibuddycam/migrations.json`` so they
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

log = logging.getLogger('pibuddycam.migrations')

MIGRATION_STATE_PATH = '/data/pibuddycam/migrations.json'

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
    tmpfiles = os.path.join(root, 'etc', 'tmpfiles.d', 'pibuddycam-samba.conf')

    os.makedirs(samba_dir, exist_ok=True)
    with open(share_conf, 'w') as f:
        f.write(
            '[sdcard]\n'
            '   path = /mnt/sdcard\n'
            '   browseable = yes\n'
            '   read only = no\n'
            '   guest ok = yes\n'
            '   force user = pibuddycam\n'
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
            '# Samba volatile state on the tmpfs /var (pibuddycam appliance).\n'
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


def _004_pi_persist_use_launcher(root):
    """Switch pi-persist.service to use the launcher for OTA-deployed code."""
    unit = os.path.join(root, 'etc', 'systemd', 'system', 'pi-persist.service')
    if not os.path.isfile(unit):
        log.warning('migrations: pi-persist.service not found at %s', unit)
        return False
    with open(unit) as f:
        content = f.read()
    old = 'ExecStart=/opt/pibuddycam/venv/bin/python /opt/pibuddycam/persist_restore.py'
    new = 'ExecStart=/opt/pibuddycam/launcher.sh persist_restore.py'
    if new in content:
        return True
    if old not in content:
        log.warning('migrations: pi-persist.service ExecStart line not found')
        return False
    content = content.replace(old, new)
    with open(unit, 'w') as f:
        f.write(content)
    subprocess.run(['systemctl', 'daemon-reload'], capture_output=True, timeout=10)
    return True


def _005_pibuddycam_priv_no_block(root):
    """Change start-camera in pibuddycam-priv to use --no-block."""
    helper = os.path.join(root, 'usr', 'libexec', 'pibuddycam', 'pibuddycam-priv')
    if not os.path.isfile(helper):
        log.warning('migrations: pibuddycam-priv not found at %s', helper)
        return False
    with open(helper) as f:
        content = f.read()
    old = 'exec "$SYSTEMCTL" start pibuddycam.target'
    new = 'exec "$SYSTEMCTL" --no-block start pibuddycam.target'
    if new in content:
        return True
    if old not in content:
        log.warning('migrations: pibuddycam-priv start-camera line not found')
        return False
    content = content.replace(old, new)
    with open(helper, 'w') as f:
        f.write(content)
    return True


def _006_runtime_directory(root):
    """Give pibuddycam.service the service-owned runtime dir (WP-UI2/AC-4).

    The bounded local control socket lives under ``/run/pibuddycam``. ``/run`` is
    root-owned, so systemd must create that directory; ``RuntimeDirectory=``
    does it with the service account as owner. This is the OTA catch-up path for
    devices whose image predates the unit change; the factory unit in
    ``app/systemd/pibuddycam.service`` already carries it.
    """
    unit = os.path.join(root, 'etc', 'systemd', 'system', 'pibuddycam.service')
    if not os.path.isfile(unit):
        log.warning('migrations: pibuddycam.service not found at %s', unit)
        return False
    with open(unit) as f:
        content = f.read()
    if 'RuntimeDirectory=pibuddycam' in content:
        return True
    marker = 'User=pibuddycam\n'
    if marker not in content:
        log.warning('migrations: pibuddycam.service User= line not found')
        return False
    addition = (
        'User=pibuddycam\n'
        '# WP-UI2/AC-4: service-owned runtime dir for the local control socket.\n'
        'RuntimeDirectory=pibuddycam\n'
        'RuntimeDirectoryMode=0750\n'
    )
    content = content.replace(marker, addition, 1)
    with open(unit, 'w') as f:
        f.write(content)
    subprocess.run(['systemctl', 'daemon-reload'], capture_output=True, timeout=10)
    return True


def _007_pibuddycam_priv_system_verbs(root):
    """Add the WP-UI7 check-update/reboot verbs to an older pibuddycam-priv.

    The fixed-verb root helper is image-owned, so an OTA application release
    cannot replace it. This is the OTA catch-up path for devices whose installed
    helper predates the System view's report-only update check and reboot
    actions. The verbs are inserted before the ``*)`` fallback so dispatch order
    is preserved; an already-patched helper is a no-op success.

    This is a ROOT change and needs a reboot (or a re-run of the boot
    migrations) to take effect on a live device.
    """
    helper = os.path.join(root, 'usr', 'libexec', 'pibuddycam', 'pibuddycam-priv')
    if not os.path.isfile(helper):
        log.warning('migrations: pibuddycam-priv not found at %s', helper)
        return False
    with open(helper) as f:
        content = f.read()

    additions = {
        'check-update': (
            '   check-update)\n'
            '      # Report-only: pibuddycam-updater.service runs updater_install.py check.\n'
            '      exec "$SYSTEMCTL" start pibuddycam-updater.service\n'
            '      ;;\n'
        ),
        'reboot': (
            '   reboot)\n'
            '      exec "$SYSTEMCTL" reboot\n'
            '      ;;\n'
        ),
    }
    if all(f'{verb})' in content for verb in additions):
        return True

    marker = '   *)\n      exit 2'
    if marker not in content:
        log.warning('migrations: pibuddycam-priv fallback marker not found')
        return False
    insertion = ''.join(
        block for verb, block in additions.items()
        if f'{verb})' not in content
    )
    content = content.replace(marker, insertion + marker, 1)
    with open(helper, 'w') as f:
        f.write(content)
    return True


# -- registry (append only; never reorder or rename shipped entries) -------- #

MIGRATIONS = [
    ('001_create_mnt_sdcard', _001_create_mnt_sdcard),
    ('002_install_samba_config', _002_install_samba_config),
    ('003_mask_console_setup', _003_mask_console_setup),
    ('004_pi_persist_use_launcher', _004_pi_persist_use_launcher),
    ('005_pibuddycam_priv_no_block', _005_pibuddycam_priv_no_block),
    ('006_runtime_directory', _006_runtime_directory),
    ('007_pibuddycam_priv_system_verbs', _007_pibuddycam_priv_system_verbs),
]


# -- runner ----------------------------------------------------------------- #


_UNSET = object()   # resolved at call time so tests can redirect the state file


def _load_state(path=_UNSET):
    if path is _UNSET:
        path = MIGRATION_STATE_PATH
    try:
        with open(path) as f:
            data = json.load(f)
        return set(data.get('applied', []))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()


def _save_state(applied, path=_UNSET):
    if path is _UNSET:
        path = MIGRATION_STATE_PATH
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


def run_pending(root='/', state_path=_UNSET):
    """Apply any migrations not yet recorded in the state file.

    Returns the list of newly applied migration names.  ROOT is only
    remounted when there is at least one pending migration.
    """
    if state_path is _UNSET:
        state_path = MIGRATION_STATE_PATH
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
