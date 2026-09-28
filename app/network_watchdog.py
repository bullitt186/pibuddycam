"""Root watchdog: bring the setup hotspot up when the Wi-Fi has been gone too long.

The console's network change reverts itself, but that only covers a change the console
made. If the router is replaced, the Wi-Fi password changes or the access point dies
later, the camera would stay unreachable until someone opens the case. This oneshot,
started every minute by ``pibuddycam-network-watchdog.timer``, counts how long the
station link has been unusable and, past a threshold, starts the (open) setup hotspot so
the device can be reached and fixed from a phone.

Rules
-----
* Only a claimed device is watched: without the ``pibuddycam-station`` profile the
  setup hotspot is already the intended state and nothing is done.
* "Unusable" means no activated link with an IPv4 address and a default route.
* Default threshold is 10 minutes (``PIBUDDYCAM_NETWORK_WATCHDOG_MINUTES``; ``0`` disables
  the watchdog). A healthy link resets the count.
* A running console network change (result ``applying``) is never disturbed.
* While the hotspot is up the watchdog retries the station profile every 10 minutes;
  when it comes up healthy the hotspot is stopped, otherwise the hotspot is restored.

The counters live in root-only ``/run`` (they must not survive a reboot: a fresh boot
gets a fresh chance to join). The result document (``hotspot`` / ``applied``) is the
same secret-free file the console reads. Stdlib only; importing runs no command, and
every side effect is injectable.
"""
import json
import logging
import os
import sys
import time

import network_apply
import network_settings
import wifi_station

log = logging.getLogger('pibuddycam.network_watchdog')

STATE_PATH = '/run/pibuddycam-network-watchdog.json'
ENV_MINUTES = 'PIBUDDYCAM_NETWORK_WATCHDOG_MINUTES'
DEFAULT_MINUTES = 10
RETRY_MINUTES = 10
RECOVERY_TIMEOUT_SECONDS = 30.0

# Outcomes (returned for tests and logged).
DISABLED = 'disabled'
UNCLAIMED = 'unclaimed'
BUSY = 'busy'
HEALTHY = 'healthy'
WAITING = 'waiting'
HOTSPOT_STARTED = 'hotspot-started'
HOTSPOT_HELD = 'hotspot-held'
RECOVERED = 'recovered'
RECOVERY_FAILED = 'recovery-failed'


def threshold_minutes(env=None):
    """Minutes without a usable link before the hotspot starts (0 disables)."""
    raw = (os.environ if env is None else env).get(ENV_MINUTES, '')
    try:
        value = int(str(raw).strip())
    except ValueError:
        return DEFAULT_MINUTES
    return value if 0 <= value <= 24 * 60 else DEFAULT_MINUTES


def load_state(path=STATE_PATH):
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.loads(handle.read(1024))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state, path=STATE_PATH):
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(state, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as e:
        log.warning(f'network_watchdog: could not save state: {e.__class__.__name__}')


def _start_hotspot():
    import hotspot_ctl
    return bool(getattr(hotspot_ctl.start(), 'ok', False))


def _stop_hotspot():
    import hotspot_ctl
    return bool(getattr(hotspot_ctl.stop(), 'ok', False))


def tick(*, runner=None, clock=time.time, minutes=None, state_path=STATE_PATH,
         result_path=None, ifname=wifi_station.DEFAULT_IFNAME,
         start_hotspot=_start_hotspot, stop_hotspot=_stop_hotspot,
         sleep=time.sleep, monotonic=time.monotonic):
    """One watchdog pass; returns the outcome constant."""
    runner = runner or network_apply._default_runner
    minutes = threshold_minutes() if minutes is None else minutes
    if minutes <= 0:
        return DISABLED
    result_path = result_path or network_apply.RESULT_PATH

    exists, _ssid, _wpa = network_apply._profile_info(runner)
    if not exists:
        return UNCLAIMED
    current = network_settings.read_result(result_path, now=clock)
    if current is not None and current['state'] == network_settings.STATE_APPLYING:
        return BUSY

    state = load_state(state_path)
    now = clock()

    if state.get('hotspot'):
        if now - float(state.get('last_retry', now)) < RETRY_MINUTES * 60:
            return HOTSPOT_HELD
        return _retry_station(
            runner, state, state_path, result_path, ifname, start_hotspot,
            stop_hotspot, clock, sleep, monotonic)

    ready, _gateway = network_apply._link_ready(runner, ifname, '')
    if ready:
        if state:
            save_state({}, state_path)
        return HEALTHY

    down_since = state.get('down_since')
    if not isinstance(down_since, (int, float)) or down_since > now:
        save_state({'down_since': now}, state_path)
        return WAITING
    if now - down_since < minutes * 60:
        return WAITING

    log.warning(f'network_watchdog: no usable network for {minutes} min; '
                'starting the setup hotspot')
    try:
        started = start_hotspot()
    except Exception as e:  # noqa: BLE001 - the watchdog must never raise
        log.warning(f'network_watchdog: hotspot start failed: {e.__class__.__name__}')
        started = False
    if not started:
        # Try again next minute instead of waiting another full threshold.
        return WAITING
    save_state({'hotspot': True, 'last_retry': now, 'since': down_since}, state_path)
    network_apply.write_result(
        network_settings.STATE_HOTSPOT,
        f'no network for {minutes} minutes; the setup hotspot was started',
        result_path, clock)
    return HOTSPOT_STARTED


def _retry_station(runner, state, state_path, result_path, ifname, start_hotspot,
                   stop_hotspot, clock, sleep, monotonic):
    """While the hotspot holds wlan0, give the station profile another chance."""
    now = clock()
    network_apply._text(runner, ['nmcli', 'connection', 'up', wifi_station.CONNECTION_NAME])
    healthy, _reason = network_apply.wait_healthy(
        runner, ifname=ifname, timeout=RECOVERY_TIMEOUT_SECONDS, sleep=sleep,
        monotonic=monotonic)
    if healthy:
        try:
            stop_hotspot()
        except Exception as e:  # noqa: BLE001
            log.warning(f'network_watchdog: hotspot stop failed: {e.__class__.__name__}')
        save_state({}, state_path)
        network_apply.write_result(
            network_settings.STATE_APPLIED, 'the network came back; hotspot stopped',
            result_path, clock)
        return RECOVERED
    try:
        start_hotspot()
    except Exception as e:  # noqa: BLE001
        log.warning(f'network_watchdog: hotspot restore failed: {e.__class__.__name__}')
    state['last_retry'] = now
    save_state(state, state_path)
    return RECOVERY_FAILED


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        stream=sys.stderr,
    )
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ['tick']:
        print('usage: network_watchdog.py tick', file=sys.stderr)
        return 2
    outcome = tick()
    log.info(f'network_watchdog: {outcome}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
