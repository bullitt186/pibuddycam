"""Write the systemd-timesyncd drop-in for the console's NTP setting (root).

Resolution order, first non-empty wins:

1. the user-configured servers in ``device.toml [network] ntp_servers``;
2. the servers the DHCP server advertised (option 42), which the NetworkManager
   dispatcher hands over (``ntp_apply.py dhcp <servers>``) and which are kept in
   root-only ``/run`` so a later ``apply`` can fall back to them;
3. nothing: the drop-in is removed and timesyncd uses Debian's ``FallbackNTP``
   pool unchanged.

The drop-in lives in ``/run/systemd/timesyncd.conf.d`` (tmpfs, regenerated at boot
from ``persist_restore``), and timesyncd is only restarted when its content
actually changed, so a DHCP renewal storm cannot restart it repeatedly.

Every server is re-validated here with the same rule as the console
(:func:`config_schema.validate_ntp_servers`); a bad entry is dropped, never
written. Stdlib only; importing runs no command.
"""
import logging
import os
import subprocess
import sys

import config_schema

log = logging.getLogger('pibuddycam.ntp_apply')

DROPIN_PATH = '/run/systemd/timesyncd.conf.d/50-pibuddycam.conf'
DHCP_STATE_PATH = '/run/pibuddycam-ntp-dhcp'
UNIT = 'systemd-timesyncd'


def _clean(servers):
    """Keep valid, unique entries (max 3); invalid ones are dropped."""
    kept = []
    for item in servers or []:
        try:
            checked = config_schema.validate_ntp_servers([item])
        except config_schema.ConfigError:
            continue
        for name in checked:
            if name not in kept:
                kept.append(name)
    return kept[:config_schema.MAX_NTP_SERVERS]


def resolve_servers(custom, dhcp):
    """Return the servers timesyncd should use (custom, then DHCP, else none)."""
    return _clean(custom) or _clean(dhcp)


def render(servers):
    return '[Time]\nNTP=' + ' '.join(servers) + '\n'


def write_dropin(servers, path=DROPIN_PATH):
    """Write or remove the drop-in; return True when its content changed."""
    current = None
    try:
        with open(path, encoding='utf-8') as handle:
            current = handle.read()
    except OSError:
        pass
    if not servers:
        if current is None:
            return False
        try:
            os.remove(path)
        except OSError:
            return False
        return True
    content = render(servers)
    if current == content:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        handle.write(content)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True


def read_dhcp(path=DHCP_STATE_PATH):
    try:
        with open(path, encoding='utf-8') as handle:
            return handle.read(1024).split()
    except OSError:
        return []


def write_dhcp(servers, path=DHCP_STATE_PATH):
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(' '.join(servers) + '\n')
    os.chmod(path, 0o600)


def configured_servers(device_path=None):
    """The user-configured servers, or ``[]`` when unset/unreadable."""
    try:
        device = (config_schema.load_device(device_path) if device_path
                  else config_schema.load_device())
    except config_schema.ConfigError:
        return []
    return list((device.get('network') or {}).get('ntp_servers') or [])


def _restart(runner=subprocess.run):
    try:
        runner(['systemctl', 'try-restart', UNIT], check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning(f'ntp_apply: could not restart {UNIT}: {e.__class__.__name__}')


def apply(*, dhcp=None, device_path=None, dropin_path=DROPIN_PATH,
          dhcp_path=DHCP_STATE_PATH, restart=_restart):
    """Resolve, write the drop-in and restart timesyncd if it changed.

    ``dhcp`` is the list handed over by the dispatcher (``None`` means "reuse the
    stored one"). Returns the servers now in effect.
    """
    if dhcp is not None:
        dhcp = _clean(dhcp)
        try:
            write_dhcp(dhcp, dhcp_path)
        except OSError as e:
            log.warning(f'ntp_apply: could not store DHCP servers: {e.__class__.__name__}')
    else:
        dhcp = read_dhcp(dhcp_path)
    servers = resolve_servers(configured_servers(device_path), dhcp)
    try:
        changed = write_dropin(servers, dropin_path)
    except OSError as e:
        log.warning(f'ntp_apply: could not write the drop-in: {e.__class__.__name__}')
        return servers
    if changed:
        restart()
    return servers


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        stream=sys.stderr,
    )
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ('apply', 'dhcp'):
        print('usage: ntp_apply.py apply | dhcp [server ...]', file=sys.stderr)
        return 2
    apply(dhcp=args[1:] if args[0] == 'dhcp' else None)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
