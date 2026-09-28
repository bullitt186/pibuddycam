"""Root transaction behind the console's Network settings.

Runs as ``pibuddycam-network-apply.service`` (root oneshot), started through the
fixed-verb helper (``pibuddycam-priv network-apply``), so a dropped HTTP connection
or an admin restart can never abandon it half-way. The request is the JSON the
helper stored in ``/run/pibuddycam-network-request.json`` (root-only tmpfs); it is
read and deleted first thing, so the PSK never outlives the read.

Transaction (auto-revert against lock-out):

1. snapshot the stored ``pibuddycam-station`` keyfile;
2. apply the new profile (:func:`wifi_station.apply`; the PSK goes through the
   ``0600`` passwd-file only, an empty PSK on an unchanged SSID keeps the stored
   key);
3. wait up to :data:`HEALTH_TIMEOUT_SECONDS` for an activated link with an IPv4
   address and a default route, and, for a static profile, a reachable gateway;
4. on failure restore the snapshot and re-activate it; when even that does not
   come up, start the setup hotspot so the device stays reachable.

The result (``applying|applied|reverted|hotspot`` plus a bounded reason) is written
to ``/run/pibuddycam/network-result.json``. It never contains the PSK or the SSID
password material; reasons are fixed strings.

Stdlib only; importing runs no command. Every side effect (runner, sleep, clock,
files, hotspot) is injectable for tests.
"""
import json
import logging
import os
import subprocess
import sys
import time

import network_settings
import wifi_station

log = logging.getLogger('pibuddycam.network_apply')

REQUEST_PATH = '/run/pibuddycam-network-request.json'
RESULT_PATH = network_settings.RESULT_PATH
KEYFILE_PATH = (
    f'/etc/NetworkManager/system-connections/{wifi_station.CONNECTION_NAME}.nmconnection'
)

HEALTH_TIMEOUT_SECONDS = 60.0
RESTORE_TIMEOUT_SECONDS = 30.0
POLL_SECONDS = 2.0
_RUN_TIMEOUT = 15.0


def _default_runner(args, timeout, input=None):
    return subprocess.run(
        list(args), capture_output=True, text=True, timeout=timeout,
        check=False, input=input,
    )


def _text(runner, args):
    try:
        result = runner(list(args), _RUN_TIMEOUT)
    except Exception:  # noqa: BLE001 - probes must never raise
        return None
    if getattr(result, 'returncode', 1) != 0:
        return None
    out = getattr(result, 'stdout', '') or ''
    return out.decode('utf-8', 'replace') if isinstance(out, bytes) else out


def write_result(state, reason='', path=None, clock=time.time):
    """Atomically write the secret-free result document (world-readable)."""
    path = path or RESULT_PATH
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump({'state': state, 'reason': reason, 'updated_at': clock()}, handle)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError as e:
        log.warning(f'network_apply: could not write result: {e.__class__.__name__}')


def read_request(path=None):
    """Read and delete the request; return the validated dict or raise."""
    path = path or REQUEST_PATH
    try:
        with open(path, encoding='utf-8') as handle:
            raw = handle.read(8192)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise network_settings.ValidationError('request must be an object')
    return network_settings.validate_request(body)


def _profile_info(runner):
    """Return ``(exists, ssid, has_wpa)`` of the stored station profile."""
    out = _text(runner, [
        'nmcli', '-t', '-f', '802-11-wireless.ssid,802-11-wireless-security.key-mgmt',
        'connection', 'show', wifi_station.CONNECTION_NAME,
    ])
    if out is None:
        return False, '', False
    fields = network_settings._terse_map(out)
    return (
        True,
        network_settings._first(fields, '802-11-wireless.ssid'),
        bool(network_settings._first(fields, '802-11-wireless-security.key-mgmt')),
    )


def _link_ready(runner, ifname, expected):
    """True when the link is activated with an IPv4 address and default gateway."""
    out = _text(runner, [
        'nmcli', '-t', '-f', 'GENERAL.STATE,IP4.ADDRESS,IP4.GATEWAY',
        'device', 'show', ifname,
    ])
    if not out:
        return False, ''
    fields = network_settings._terse_map(out)
    state = network_settings._first(fields, 'GENERAL.STATE')
    address = network_settings._first(fields, 'IP4.ADDRESS').partition('/')[0]
    gateway = network_settings._first(fields, 'IP4.GATEWAY')
    if not state.startswith('100') or not address or not gateway:
        return False, ''
    if expected and address != expected:
        return False, ''
    return True, gateway


def _ping(runner, gateway):
    try:
        result = runner(['ping', '-c', '1', '-W', '2', gateway], 8.0)
    except Exception:  # noqa: BLE001
        return False
    return getattr(result, 'returncode', 1) == 0


def wait_healthy(runner, *, ifname, expected_address='', require_ping=False,
                 timeout=HEALTH_TIMEOUT_SECONDS, sleep=time.sleep,
                 monotonic=time.monotonic):
    """Poll until the link is usable; returns ``(ok, reason)``."""
    deadline = monotonic() + timeout
    reason = 'no IPv4 address or default route'
    while True:
        ready, gateway = _link_ready(runner, ifname, expected_address)
        if ready:
            if not require_ping:
                # Best-effort ping only to prefer a confirmed path; never fatal.
                _ping(runner, gateway)
                return True, ''
            if _ping(runner, gateway):
                return True, ''
            reason = 'gateway unreachable'
        if monotonic() >= deadline:
            return False, reason
        sleep(POLL_SECONDS)


def _snapshot(path):
    try:
        with open(path, 'rb') as handle:
            return handle.read()
    except OSError:
        return None


def _restore(path, snapshot, runner):
    """Put the snapshot back (or drop the new profile) and reload NM."""
    try:
        if snapshot is None:
            _text(runner, ['nmcli', 'connection', 'delete', wifi_station.CONNECTION_NAME])
        else:
            tmp = path + '.restore'
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(snapshot)
            os.replace(tmp, path)
    except OSError as e:
        log.warning(f'network_apply: snapshot restore failed: {e.__class__.__name__}')
        return False
    _text(runner, ['nmcli', 'connection', 'reload'])
    if snapshot is not None:
        _text(runner, ['nmcli', 'connection', 'up', wifi_station.CONNECTION_NAME])
    return True


def _start_hotspot():
    import hotspot_ctl
    result = hotspot_ctl.start()
    return bool(getattr(result, 'ok', False))


def run(request, *, runner=None, ifname=wifi_station.DEFAULT_IFNAME,
        keyfile=KEYFILE_PATH, result_path=None, sleep=time.sleep,
        monotonic=time.monotonic, clock=time.time, hotspot_fn=_start_hotspot,
        health_timeout=HEALTH_TIMEOUT_SECONDS,
        restore_timeout=RESTORE_TIMEOUT_SECONDS):
    """Execute the transaction for a validated ``request``; return the state."""
    runner = runner or _default_runner
    write_result(network_settings.STATE_APPLYING, '', result_path, clock)

    exists, current_ssid, has_wpa = _profile_info(runner)
    psk = request.get('psk', '')
    keep = not psk and exists and has_wpa and current_ssid == request['ssid']
    manual = request['ipv4_method'] == 'manual'
    ipv4 = (
        {key: request[key] for key in ('address', 'prefix', 'gateway', 'dns')}
        | {'method': 'manual'}
    ) if manual else {'method': 'auto'}
    snapshot = _snapshot(keyfile)

    ok, reason = wifi_station.apply(
        request['ssid'], psk, ifname=ifname, runner=runner,
        ipv4=ipv4, keep_psk=keep, existing=exists,
    )
    if ok:
        ok, reason = wait_healthy(
            runner, ifname=ifname,
            expected_address=request.get('address', '') if manual else '',
            require_ping=manual, timeout=health_timeout, sleep=sleep,
            monotonic=monotonic,
        )
    if ok:
        write_result(network_settings.STATE_APPLIED, '', result_path, clock)
        return network_settings.STATE_APPLIED

    reason = reason or 'activation failed'
    log.warning(f'network_apply: apply failed ({reason}); restoring the previous profile')
    restored = _restore(keyfile, snapshot, runner)
    healthy = False
    if restored and snapshot is not None:
        healthy, _ = wait_healthy(
            runner, ifname=ifname, timeout=restore_timeout, sleep=sleep,
            monotonic=monotonic,
        )
    if healthy:
        write_result(network_settings.STATE_REVERTED, reason, result_path, clock)
        return network_settings.STATE_REVERTED
    started = False
    try:
        started = hotspot_fn()
    except Exception as e:  # noqa: BLE001 - last resort must not raise
        log.warning(f'network_apply: hotspot start failed: {e.__class__.__name__}')
    state = network_settings.STATE_HOTSPOT if started else network_settings.STATE_REVERTED
    write_result(state, reason + '; previous network did not come back', result_path, clock)
    return state


def main(argv=None):
    """Entry point of the root unit; exit 0 unless the request was unusable."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        stream=sys.stderr,
    )
    try:
        request = read_request()
    except (OSError, ValueError) as e:
        # ValueError covers JSON errors and network_settings.ValidationError.
        write_result(network_settings.STATE_REVERTED, 'invalid request')
        print(f'network_apply: unusable request ({e.__class__.__name__})', file=sys.stderr)
        return 1
    run(request)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
