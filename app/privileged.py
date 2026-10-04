"""Fixed-verb privileged operations for the appliance (WP-R1, AC-17).

The admin/provisioning UI runs as the unprivileged ``pibuddycam`` service account,
but the wizard's finish path must stop the provisioning unit, stop the setup AP,
activate station networking, and start the camera target — all root-only actions.
Rather than granting broad ``sudo`` rights, the image installs a single
fixed-verb root helper (``/usr/libexec/pibuddycam/pibuddycam-priv``) and a sudoers rule
that allows only that helper. This module is the unprivileged client of it.

Security contract
-----------------
* The verb allowlist lives here *and* in the helper; an unknown verb is rejected
  before any process is started.
* The helper is invoked as ``sudo -n /usr/libexec/pibuddycam/pibuddycam-priv <verb>``.
  The sudoers entry lists the helper without arguments, which (per sudoers
  semantics) permits any arguments — so the helper itself validates the verb.
* The Wi-Fi PSK is passed to ``wifi-station-apply`` on **stdin** and never
  appears in argv, a log line, or a failure reason.
* Every wrapper never raises: a timeout, a missing tool, or a non-zero exit is
  reported as a bounded, non-secret reason.

Stdlib only, and importing this module runs no command. The injectable
``runner(args, timeout, input=None)`` callable mirrors :mod:`wifi_station`, so
the tests replace it with a fake.
"""
import dataclasses
import logging
import subprocess
import time

import hotspot

log = logging.getLogger('pibuddycam.privileged')

#: The fixed-verb root helper installed by ``image/assets/install-factory-app.sh``.
PRIVILEGED_HELPER = '/usr/libexec/pibuddycam/pibuddycam-priv'

#: The privilege-escalation command. ``-n`` fails instead of prompting, which is
#: essential for a non-interactive service.
SUDO = 'sudo'

#: Every verb the helper understands. The helper re-validates this list.
VERBS = frozenset({
    'start-camera',
    'stop-provisioning',
    'hotspot-start',
    'hotspot-stop',
    'wifi-station-apply',
    'install-update',
    'check-update',
    'reboot',
    'rtsp-start',
    'rtsp-stop',
    'quality-restart',
    'camera-restart',
    'network-apply',
    'hostname-apply',
    'wifi-scan',
    'ntp-apply',
    'timezone-apply',
})

#: Bounded wall-clock timeout for a privileged invocation.
COMMAND_TIMEOUT_SECONDS = 60.0

#: The report-only update check downloads a signed manifest and verifies it, so
#: it needs a longer budget than a plain systemctl toggle. The manual check runs
#: on a background thread (WP-UI7), so this bounds the thread, not a request.
CHECK_TIMEOUT_SECONDS = 360.0

#: Longer budget for station activation: ``wifi_station.apply`` may run several
#: ``nmcli`` commands (add/modify/reload/up) that each carry their own bounded
#: timeout, so the outer helper timeout must exceed their worst-case sum. A
#: mismatch would SIGKILL only ``sudo`` and leave the root-side activation
#: running after the wizard had already reported failure.
STATION_TIMEOUT_SECONDS = 180.0

_REASON_MAX = 200


@dataclasses.dataclass
class PrivilegedResult:
    """Outcome of a privileged invocation (never carries a secret).

    Truthy exactly when ``ok`` is true, so :class:`setup_wizard.WizardSession`
    can treat it as the documented "returns falsy on failure" callback.
    """

    ok: bool
    reason: str = ''
    #: Captured stdout, only for verbs invoked with ``capture=True`` (wifi-scan).
    output: str = ''

    def __bool__(self):
        return bool(self.ok)


def _sanitize_reason(reason):
    """Bound and strip a reason so it can never carry control characters."""
    if not isinstance(reason, str):
        return ''
    cleaned = ''.join(
        ch for ch in reason if ch.isprintable() and ch not in '\r\n\t'
    ).strip()
    if len(cleaned) > _REASON_MAX:
        cleaned = cleaned[:_REASON_MAX]
    return cleaned


def _default_runner(args, timeout, input=None):
    """Run ``args`` with a bounded timeout, capturing text stdout/stderr.

    The single place this module touches :mod:`subprocess`; tests replace it
    with a fake so no privileged command runs on a workstation.
    """
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        input=input,
    )


def _returncode(result):
    """Best-effort return code of a runner result (missing means failure)."""
    value = getattr(result, 'returncode', 1)
    return value if isinstance(value, int) else 1


def _invoke(verb, *extra, input=None, runner=None, timeout=None, capture=False):
    """Invoke ``verb`` through the helper and normalize the outcome.

    Returns a :class:`PrivilegedResult`; never raises. ``extra`` are the verb's
    non-secret arguments (currently only the station SSID). ``input`` carries
    stdin (the PSK) and is never logged or included in ``reason``.
    """
    runner = runner or _default_runner
    if verb not in VERBS:
        return PrivilegedResult(False, 'unsupported privileged action')
    if timeout is None:
        timeout = COMMAND_TIMEOUT_SECONDS
    args = [SUDO, '-n', PRIVILEGED_HELPER, verb, *extra]
    try:
        result = runner(args, timeout, input=input)
    except subprocess.TimeoutExpired:
        return PrivilegedResult(False, 'privileged action timed out')
    except OSError:
        return PrivilegedResult(False, 'privileged helper unavailable')
    except Exception as e:  # noqa: BLE001 - privileged control must never raise
        log.warning(f'privileged: runner failed: {type(e).__name__}')
        return PrivilegedResult(False, 'privileged action failed')
    returncode = _returncode(result)
    if returncode != 0:
        return PrivilegedResult(
            False,
            _sanitize_reason(f'{verb} failed (exit {returncode})'),
        )
    output = ''
    if capture:
        raw = getattr(result, 'stdout', '') or ''
        output = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)
    return PrivilegedResult(True, '', output=output)


# --------------------------------------------------------------------------- #
# Wrappers
# --------------------------------------------------------------------------- #

def _target_coming_up(run):
    """True when ``pibuddycam.target`` is ``active`` or ``activating``."""
    try:
        probe = run(['systemctl', 'is-active', 'pibuddycam.target'], timeout=5)
    except Exception:  # noqa: BLE001 - a probe must never raise
        return False
    if _returncode(probe) == 0:
        return True
    out = getattr(probe, 'stdout', '') or ''
    if isinstance(out, bytes):
        out = out.decode('utf-8', 'replace')
    return out.strip() in ('active', 'activating')


def _start_job_queued(run):
    """True while a start job for ``pibuddycam.target`` is still queued.

    The target conflicts with the provisioning unit, so systemd keeps the start job
    waiting (the target still reads ``inactive``) until that unit has finished
    stopping, which takes several seconds.
    """
    try:
        probe = run(['systemctl', 'list-jobs', '--no-legend'], timeout=5)
    except Exception:  # noqa: BLE001 - a probe must never raise
        return False
    out = getattr(probe, 'stdout', '') or ''
    if isinstance(out, bytes):
        out = out.decode('utf-8', 'replace')
    return any(
        'pibuddycam.target' in line and 'start' in line.split()
        for line in out.splitlines())


def start_camera(runner=None, poll_timeout=30, grace_polls=20, sleep=None):
    """Start ``pibuddycam.target`` as root; returns a plain ``bool``.

    The helper issues ``systemctl --no-block start`` (the target conflicts with
    provisioning, so a blocking start deadlocks when called from within the
    provisioning service). After the non-blocking start returns, this function
    polls ``systemctl is-active`` up to *poll_timeout* seconds for the target to
    reach active. The wizard's finish callback treats a falsy result as a failed
    hand-off.

    Starting the target makes systemd stop the provisioning service this code
    runs in, and that kills the still-running ``sudo`` child, so ``_invoke`` can
    report a failure (a signal exit) although the start job was already queued.
    Treating that as a failed hand-off made the wizard restart the setup hotspot
    and tear down the Wi-Fi it had just joined. A failed ``_invoke`` therefore
    only counts as failure when the target is not coming up either: it is
    re-checked for *grace_polls* seconds before giving up. "Coming up" includes a start
    job that is still queued behind the provisioning unit's stop, when the target
    itself still reads ``inactive``; judging that as a failure restarted the hotspot
    and dropped the station link the wizard had just brought up.
    """
    sleep = sleep or time.sleep
    run = runner or _default_runner
    result = _invoke('start-camera', runner=runner)
    if not result.ok:
        for attempt in range(max(1, grace_polls)):
            if _target_coming_up(run) or _start_job_queued(run):
                log.warning(
                    'privileged: start-camera reported a failure but '
                    'pibuddycam.target is coming up (helper cut off by the '
                    'provisioning stop); treating the start as successful')
                return True
            if attempt + 1 < max(1, grace_polls):
                sleep(1)
        return False
    deadline = time.monotonic() + poll_timeout
    while time.monotonic() < deadline:
        try:
            probe = run(
                ['systemctl', 'is-active', '--quiet', 'pibuddycam.target'],
                timeout=5,
            )
            if _returncode(probe) == 0:
                return True
        except Exception:
            pass
        sleep(2)
    log.warning('privileged: pibuddycam.target did not reach active within '
                f'{poll_timeout}s after --no-block start')
    return False


def stop_provisioning(runner=None):
    """Stop ``pibuddycam-provisioning.service`` as root."""
    return _invoke('stop-provisioning', runner=runner)


def install_update(runner=None):
    """Trigger ``pibuddycam-updater-install.service`` as root.

    The helper starts the root oneshot unit, which runs the signed install
    (``updater_install.py install``); the MQTT trigger never performs the install
    itself and never handles the signing key. Returns a :class:`PrivilegedResult`.
    """
    return _invoke('install-update', runner=runner)


def check_update(runner=None):
    """Trigger the report-only signed-update check as root (WP-UI7; AC-15).

    Starts the existing ``pibuddycam-updater.service`` oneshot through the fixed-verb
    helper. That unit runs ``updater_install.py check`` against the root-owned
    manifest URL and never installs anything. The caller (the admin
    :class:`update_control.UpdateManager`) runs this on a background thread and
    polls the root-written update-state document for the result.
    """
    return _invoke('check-update', runner=runner, timeout=CHECK_TIMEOUT_SECONDS)


def reboot(runner=None):
    """Reboot the appliance through the fixed-verb helper (WP-UI7; AC-16).

    The only privileged command issued is ``systemctl reboot``; the caller
    (:mod:`device_control` through the admin reboot route) owns the rate limit
    and confirmation. Returns a :class:`PrivilegedResult`.
    """
    return _invoke('reboot', runner=runner)


def rtsp_start(runner=None):
    """Start ``pibuddycam-rtsp.service`` as root.

    The appliance's ``pibuddycam`` account cannot call ``systemctl`` directly
    (only the fixed-verb helper is in sudoers), so runtime RTSP toggles go
    through the helper. Returns a :class:`PrivilegedResult`.
    """
    return _invoke('rtsp-start', runner=runner)


def rtsp_stop(runner=None):
    """Stop ``pibuddycam-rtsp.service`` as root (see :func:`rtsp_start`)."""
    return _invoke('rtsp-stop', runner=runner)


def quality_restart(runner=None):
    """Restart the shared camera pipeline after a quality change as root.

    The helper owns the exact unit list and uses ``try-restart`` for the
    Prusa-controlled RTSP endpoint so changing quality cannot enable a stream
    whose configured mode is disabled. Returns a :class:`PrivilegedResult`.
    """
    return _invoke('quality-restart', runner=runner)


def camera_restart(runner=None):
    """Restart the camera application (``pibuddycam.service``) as root.

    Used after a Prusa token is saved in the console, so it takes effect without
    a reboot. The helper uses ``try-restart``: a stopped service stays stopped.
    An older image whose helper lacks this verb answers with a failure, which the
    caller reports as "restart required". Returns a :class:`PrivilegedResult`.
    """
    return _invoke('camera-restart', runner=runner)


def hotspot_start(runner=None):
    """Start the setup AP as root (``hotspot_ctl.py start``)."""
    return _invoke('hotspot-start', runner=runner)


def hotspot_stop(runner=None):
    """Stop the setup AP as root (``hotspot_ctl.py stop``)."""
    return _invoke('hotspot-stop', runner=runner)


def activate_station(ssid, psk, runner=None):
    """Create/activate the station profile as root; PSK passed on stdin.

    Returns a :class:`PrivilegedResult`. The SSID is the only non-secret
    argument; the PSK is stdin and never appears in argv or ``reason``.
    """
    if not isinstance(ssid, str) or not ssid.strip():
        return PrivilegedResult(False, 'wifi SSID is required')
    if psk is None:
        psk = ''
    if not isinstance(psk, str):
        return PrivilegedResult(False, 'wifi PSK must be a string')
    return _invoke(
        'wifi-station-apply', ssid.strip(), input=psk, runner=runner,
        timeout=STATION_TIMEOUT_SECONDS,
    )


def network_apply(request_json, runner=None):
    """Start the root network-apply transaction; the request goes on stdin.

    The JSON may carry the new Wi-Fi PSK, so it never appears in argv, a log
    line or ``reason``. The verb returns once the oneshot unit is queued
    (``--no-block``); the outcome is read from the root-written result file.
    """
    if not isinstance(request_json, str) or not request_json:
        return PrivilegedResult(False, 'network request is required')
    return _invoke('network-apply', input=request_json, runner=runner)


def hostname_apply(name, runner=None):
    """Set the transient hostname; the helper re-validates the RFC 1123 label."""
    if not isinstance(name, str) or not name:
        return PrivilegedResult(False, 'hostname is required')
    return _invoke('hostname-apply', name, runner=runner)


def ntp_apply(runner=None):
    """Regenerate the timesyncd drop-in from ``device.toml`` as root."""
    return _invoke('ntp-apply', runner=runner)


def timezone_apply(name, runner=None):
    """Set the operating-system time zone; the helper re-validates the IANA name."""
    if not isinstance(name, str) or not name:
        return PrivilegedResult(False, 'time zone is required')
    return _invoke('timezone-apply', name, runner=runner)


def wifi_scan(runner=None):
    """Scan for Wi-Fi networks as root; returns a list of ``{ssid, signal, secured}``.

    Returns ``None`` when the scan could not run (old image, no radio) so callers
    can tell it apart from "no networks in range" (an empty list).
    """
    import network_settings
    result = _invoke('wifi-scan', runner=runner, timeout=30.0, capture=True)
    if not result.ok:
        return None
    return network_settings.parse_scan_output(result.output)


class PrivilegedHotspot:
    """Hotspot controller whose start/stop need root, status stays read-only.

    ``start``/``stop`` route through the privileged helper (the setup AP and its
    pinned ``192.168.4.1/24`` address need NetworkManager write access);
    ``status``/``is_active`` are read-only ``nmcli`` queries and delegate to
    :mod:`hotspot` directly.
    """

    def start(self, ssid, password=None, ifname=hotspot.DEFAULT_IFNAME, runner=None):
        # The open setup AP needs no PSK, so only the interface matters; the
        # helper derives the SSID from the same device identity.
        return hotspot_start(runner=runner)

    def stop(self, ifname=hotspot.DEFAULT_IFNAME, runner=None):
        return hotspot_stop(runner=runner)

    def status(self, ifname=hotspot.DEFAULT_IFNAME, runner=None):
        return hotspot.status(ifname=ifname, runner=runner)

    def is_active(self, ifname=hotspot.DEFAULT_IFNAME, runner=None):
        return hotspot.is_active(ifname=ifname, runner=runner)
