"""Console-side network settings: validation, live status and apply control.

The admin service runs as the unprivileged ``pibuddycam`` account. Everything that
needs root goes through the fixed-verb helper (:mod:`privileged`); this module owns
what the console may say about the network:

* **Validation** of a change request (SSID, PSK, DHCP or static IPv4, hostname,
  NTP servers) before anything crosses the privilege boundary.
* **Live status** read unprivileged from NetworkManager (``nmcli``) and
  ``timedatectl``. NetworkManager hides secrets from non-secret reads, so the PSK
  is never part of the status. **[assumption]** the service account may read the
  non-secret connection settings; verify on hardware.
* **Apply control** (:class:`NetworkController`): one transaction at a time, the
  request is handed to the root ``network_apply`` unit, the result comes back
  through a secret-free file, and the new PSK is persisted to ``secrets.toml``
  only after the result says ``applied``.

Stdlib only; importing runs no command. Every command runner is injectable.
"""
import ipaddress
import json
import logging
import re
import subprocess
import threading
import time

import config_schema
import timezone
import wifi_station

log = logging.getLogger('pibuddycam.network_settings')

#: Root-written, secret-free result of the last apply transaction.
RESULT_PATH = '/run/pibuddycam/network-result.json'

#: States the root transaction reports.
STATE_APPLYING = 'applying'
STATE_APPLIED = 'applied'
STATE_REVERTED = 'reverted'
STATE_HOTSPOT = 'hotspot'
RESULT_STATES = (STATE_APPLYING, STATE_APPLIED, STATE_REVERTED, STATE_HOTSPOT)

#: An ``applying`` result older than this is a dead transaction, not a busy one.
APPLY_STALE_SECONDS = 180.0

#: Minimum spacing between Wi-Fi scans (each scan briefly disturbs the radio).
SCAN_MIN_INTERVAL_SECONDS = 10.0

MAX_SSID_BYTES = 32
MIN_PSK_LENGTH = wifi_station.MIN_PSK_LENGTH
MAX_PSK_LENGTH = wifi_station.MAX_PSK_LENGTH
MAX_DNS_SERVERS = 3
MIN_PREFIX = 1
MAX_PREFIX = 30

_HOSTNAME_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')
_REASON_MAX = 200
_STATUS_TIMEOUT_SECONDS = 5.0


class ValidationError(ValueError):
    """A network request field is invalid; the message is safe to show."""


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def validate_hostname(value):
    """Return the lowercase RFC 1123 label or raise :class:`ValidationError`."""
    if not isinstance(value, str):
        raise ValidationError('hostname must be a string')
    name = value.strip().lower()
    if not _HOSTNAME_RE.match(name):
        raise ValidationError(
            'hostname must be 1-63 letters, digits or hyphens, '
            'and must not start or end with a hyphen')
    return name


def validate_ntp_servers(value):
    """Validate the NTP server list (max 3 hostnames or IPv4 addresses)."""
    if value is None:
        return []
    try:
        return config_schema.validate_ntp_servers(value)
    except config_schema.ValidationError as e:
        raise ValidationError(str(e).replace('network.ntp_servers', 'NTP servers')) from None


def _ipv4(value, label):
    if not isinstance(value, str):
        raise ValidationError(f'{label} must be an IPv4 address')
    try:
        return ipaddress.IPv4Address(value.strip())
    except ValueError:
        raise ValidationError(f'{label} must be an IPv4 address') from None


def validate_request(body):
    """Validate a ``PUT /api/network`` body; return the normalized request.

    Fields: ``ssid`` (required), ``psk`` (empty keeps the stored key when the SSID
    is unchanged), ``ipv4_method`` (``auto``|``manual``) and, for ``manual``,
    ``address``, ``prefix``, ``gateway`` and ``dns`` (up to three servers).
    Raises :class:`ValidationError` with a bounded, non-secret message.
    """
    ssid = body.get('ssid')
    if not isinstance(ssid, str) or not ssid.strip():
        raise ValidationError('Wi-Fi network name is required')
    ssid = ssid.strip()
    if len(ssid.encode('utf-8')) > MAX_SSID_BYTES:
        raise ValidationError('Wi-Fi network name must be at most 32 bytes')
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in ssid):
        raise ValidationError('Wi-Fi network name contains control characters')

    psk = body.get('psk', '')
    if psk is None:
        psk = ''
    if not isinstance(psk, str):
        raise ValidationError('Wi-Fi password must be a string')
    if psk and not MIN_PSK_LENGTH <= len(psk) <= MAX_PSK_LENGTH:
        raise ValidationError(
            f'Wi-Fi password must be {MIN_PSK_LENGTH}-{MAX_PSK_LENGTH} characters')

    method = body.get('ipv4_method', 'auto')
    if method not in ('auto', 'manual'):
        raise ValidationError('ipv4_method must be auto or manual')

    request = {'ssid': ssid, 'psk': psk, 'ipv4_method': method}
    if method == 'manual':
        address = _ipv4(body.get('address'), 'IP address')
        prefix = body.get('prefix')
        if type(prefix) is not int or not MIN_PREFIX <= prefix <= MAX_PREFIX:
            raise ValidationError(f'prefix length must be {MIN_PREFIX}-{MAX_PREFIX}')
        gateway = _ipv4(body.get('gateway'), 'gateway')
        network = ipaddress.IPv4Network(f'{address}/{prefix}', strict=False)
        if gateway not in network:
            raise ValidationError('gateway must be inside the subnet')
        if gateway == address:
            raise ValidationError('gateway must differ from the IP address')
        if address in (network.network_address, network.broadcast_address):
            raise ValidationError('IP address must be a host address of the subnet')
        dns = body.get('dns', [])
        if dns is None:
            dns = []
        if not isinstance(dns, list) or len(dns) > MAX_DNS_SERVERS:
            raise ValidationError(f'DNS accepts at most {MAX_DNS_SERVERS} servers')
        servers = []
        for item in dns:
            server = str(_ipv4(item, 'DNS server'))
            if server not in servers:
                servers.append(server)
        request.update({
            'address': str(address), 'prefix': prefix,
            'gateway': str(gateway), 'dns': servers,
        })
    return request


# --------------------------------------------------------------------------- #
# Live status (unprivileged reads)
# --------------------------------------------------------------------------- #

def _default_runner(args, timeout, input=None):
    return subprocess.run(
        list(args), capture_output=True, text=True, timeout=timeout,
        check=False, input=input,
    )


def _run_text(runner, args):
    """Run a read-only command; return stdout or ``''`` on any failure."""
    try:
        result = runner(list(args), _STATUS_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - status reads must never raise
        return ''
    if getattr(result, 'returncode', 1) != 0:
        return ''
    out = getattr(result, 'stdout', '') or ''
    return out.decode('utf-8', 'replace') if isinstance(out, bytes) else out


def _split_terse(line):
    """Split ``key:value`` from ``nmcli -t`` output, honouring ``\\:`` escapes."""
    key, sep, value = line.partition(':')
    if not sep:
        return None, ''
    return key.strip(), value.replace('\\:', ':').replace('\\\\', '\\').strip()


def _terse_map(text):
    """Return ``{key: [values]}`` for ``nmcli -t`` ``KEY[n]:value`` lines."""
    fields = {}
    for line in text.splitlines():
        key, value = _split_terse(line)
        if not key or not value:
            continue
        key = re.sub(r'\[\d+\]$', '', key)
        fields.setdefault(key, []).append(value)
    return fields


def _first(fields, key):
    values = fields.get(key) or []
    return values[0] if values else ''


def read_link_status(runner=None, ifname=wifi_station.DEFAULT_IFNAME):
    """Return the live wlan0 IPv4 view: address, gateway, DNS, SSID and mode."""
    runner = runner or _default_runner
    device = _terse_map(_run_text(runner, [
        'nmcli', '-t', '-f', 'GENERAL.STATE,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS',
        'device', 'show', ifname,
    ]))
    profile = _terse_map(_run_text(runner, [
        'nmcli', '-t', '-f',
        '802-11-wireless.ssid,ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns',
        'connection', 'show', wifi_station.CONNECTION_NAME,
    ]))
    address = ''
    prefix = None
    raw = _first(device, 'IP4.ADDRESS')
    if raw:
        host, _, length = raw.partition('/')
        address = host
        prefix = int(length) if length.isdigit() else None
    method = _first(profile, 'ipv4.method')
    configured = {'method': method if method in ('auto', 'manual') else 'auto'}
    stored = _first(profile, 'ipv4.addresses')
    if configured['method'] == 'manual' and stored:
        host, _, length = stored.partition('/')
        configured.update({
            'address': host,
            'prefix': int(length) if length.isdigit() else None,
            'gateway': _first(profile, 'ipv4.gateway'),
            'dns': [item.strip() for item in _first(profile, 'ipv4.dns').split(',')
                    if item.strip()],
        })
    return {
        'connected': bool(address),
        'state': _first(device, 'GENERAL.STATE'),
        'ssid': _first(profile, '802-11-wireless.ssid'),
        'address': address,
        'prefix': prefix,
        'gateway': _first(device, 'IP4.GATEWAY'),
        'dns': device.get('IP4.DNS', [])[:MAX_DNS_SERVERS],
        'ipv4': configured,
    }


def read_time_status(runner=None, now=time.time):
    """Return ``{synchronized, server, now, timezone}`` from ``timedatectl``."""
    runner = runner or _default_runner
    synced = _run_text(runner, [
        'timedatectl', 'show', '-p', 'NTPSynchronized', '--value']).strip()
    server = _run_text(runner, [
        'timedatectl', 'show-timesync', '-p', 'ServerName', '--value']).strip()
    zone = _run_text(runner, ['timedatectl', 'show', '-p', 'Timezone', '--value']).strip()
    return {
        'synchronized': synced == 'yes',
        'server': server[:253],
        'now': int(now()),
        'timezone': zone if timezone.valid_zone_name(zone) else '',
    }


def read_result(path=RESULT_PATH, now=time.time):
    """Return the last apply result or ``None``; an ancient ``applying`` is stale."""
    try:
        with open(path, encoding='utf-8') as handle:
            raw = handle.read(4096)
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('state') not in RESULT_STATES:
        return None
    view = {
        'state': data['state'],
        'reason': _bounded(data.get('reason')),
        'updated_at': data.get('updated_at') if isinstance(
            data.get('updated_at'), (int, float)) else None,
    }
    if view['state'] == STATE_APPLYING and view['updated_at'] is not None:
        if now() - view['updated_at'] > APPLY_STALE_SECONDS:
            view['state'] = STATE_REVERTED
            view['reason'] = 'apply did not finish'
    return view


def _bounded(value):
    if not isinstance(value, str):
        return ''
    cleaned = ''.join(ch for ch in value if ch.isprintable()).strip()
    return cleaned[:_REASON_MAX]


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #

class NetworkController:
    """Console-side owner of network apply, hostname, scan and status.

    All I/O is injected: ``runner`` for unprivileged ``nmcli``/``timedatectl``
    reads, ``apply_fn``/``hostname_fn``/``scan_fn``/``ntp_fn`` for the privileged
    verbs (each returns a :class:`privileged.PrivilegedResult`-like object, scan
    returns a list or ``None``), and the device/secrets paths for persistence.
    """

    def __init__(self, *, runner=None, apply_fn=None, hostname_fn=None,
                 scan_fn=None, ntp_fn=None, device_path=None,
                 secrets_path=None, result_path=RESULT_PATH,
                 clock=time.time, monotonic=time.monotonic):
        self._runner = runner or _default_runner
        self._apply_fn = apply_fn
        self._hostname_fn = hostname_fn
        self._scan_fn = scan_fn
        self._ntp_fn = ntp_fn
        self._device_path = device_path if device_path is not None \
            else config_schema.DEVICE_TOML_PATH
        self._secrets_path = secrets_path if secrets_path is not None \
            else config_schema.SECRETS_TOML_PATH
        self._result_path = result_path
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._pending = None  # {'ssid', 'psk', 'at'} awaiting the root result
        self._scan_lock = threading.Lock()
        self._scan_cache = {'at': None, 'networks': []}
        self._scan_running = False

    # -- status ---------------------------------------------------------- #

    def _result_for_pending(self):
        """The result written since the pending request, else ``None``."""
        result = read_result(self._result_path, now=self._clock)
        pending = self._pending
        if result is None:
            return None
        if pending is not None and (
                result['updated_at'] is None or result['updated_at'] < pending['wall']):
            return None
        return result

    def busy(self):
        """True while a root apply transaction is (believed to be) running."""
        result = read_result(self._result_path, now=self._clock)
        if result is not None and result['state'] == STATE_APPLYING:
            return True
        pending = self._pending
        return (
            pending is not None
            and self._monotonic() - pending['at'] < 30.0
            and self._result_for_pending() is None
        )

    def status(self):
        """Return the live network + time + last-result view (never a secret)."""
        self._finalize()
        link = read_link_status(self._runner)
        device = self._load_device()
        secrets_cfg = self._load_secrets()
        wifi = secrets_cfg.get('wifi') if isinstance(secrets_cfg, dict) else None
        return {
            'link': link,
            'hostname': (device.get('admin') or {}).get('hostname', ''),
            'ntp_servers': list((device.get('network') or {}).get('ntp_servers', [])),
            'psk_set': bool(isinstance(wifi, dict) and wifi.get('psk')),
            'time': read_time_status(self._runner, now=self._clock),
            'result': read_result(self._result_path, now=self._clock),
            'busy': self.busy(),
        }

    def current_address(self):
        """The wlan0 IPv4 address, or ``''`` (a cheap read for ``/api/system``)."""
        try:
            return read_link_status(self._runner).get('address', '')
        except Exception:  # noqa: BLE001 - never break a system view
            return ''

    # -- apply ----------------------------------------------------------- #

    def apply(self, request):
        """Hand a validated request to the root unit.

        Returns ``(status, payload)`` where status is an HTTP-like code:
        202 accepted, 409 busy, 501 helper missing, 502 helper failed.
        """
        if self._apply_fn is None:
            return 501, {'error': 'network changes are unavailable'}
        with self._lock:
            if self.busy():
                return 409, {'error': 'a network change is already in progress'}
            result = self._apply_fn(json.dumps(request))
            if not result.ok:
                if _is_unknown_verb(result):
                    return 501, {'error': 'network changes need a newer image'}
                return 502, {'error': 'network change could not be started'}
            self._pending = {
                'ssid': request['ssid'], 'psk': request.get('psk', ''),
                'at': self._monotonic(), 'wall': self._clock(),
            }
        expected = request.get('address', '')
        return 202, {'accepted': True, 'expected_address': expected}

    def _finalize(self):
        """Persist the PSK once the root result says the change was applied."""
        pending = self._pending
        if pending is None:
            return
        result = self._result_for_pending()
        if result is None or result['state'] == STATE_APPLYING:
            if self._monotonic() - pending['at'] > APPLY_STALE_SECONDS:
                self._pending = None
            return
        self._pending = None
        if result['state'] != STATE_APPLIED or not pending['psk']:
            return
        try:
            device = config_schema.load_device(self._device_path)
            secrets_cfg = config_schema.load_secrets(self._secrets_path)
            wifi = secrets_cfg.get('wifi')
            wifi = dict(wifi) if isinstance(wifi, dict) else {}
            wifi['psk'] = pending['psk']
            secrets_cfg['wifi'] = wifi
            config_schema.save_pair(
                device, secrets_cfg, self._device_path, self._secrets_path)
        except config_schema.ConfigError:
            log.warning('network: could not persist the Wi-Fi key after apply')

    # -- hostname / ntp -------------------------------------------------- #

    def set_hostname(self, name):
        """Apply and persist the hostname. Returns ``(status, payload)``."""
        if self._hostname_fn is None:
            return 501, {'error': 'hostname changes are unavailable'}
        result = self._hostname_fn(name)
        if not result.ok:
            if _is_unknown_verb(result):
                return 501, {'error': 'hostname changes need a newer image'}
            return 502, {'error': 'hostname could not be applied'}
        if not self._update_device(lambda cfg: cfg.setdefault('admin', {}).update(
                {'hostname': name})):
            return 500, {'error': 'could not save the hostname'}
        return 200, {'ok': True, 'hostname': name, 'reboot_recommended': True}

    def set_ntp_servers(self, servers):
        """Persist NTP servers then ask the root helper to apply them."""
        if not self._update_device(lambda cfg: cfg.setdefault('network', {}).update(
                {'ntp_servers': list(servers)})):
            return 500, {'error': 'could not save the NTP servers'}
        applied = None
        if self._ntp_fn is not None:
            result = self._ntp_fn()
            applied = bool(result.ok)
        return 200, {'ok': True, 'ntp_servers': list(servers), 'applied': applied}

    # -- scan ------------------------------------------------------------ #

    def scan(self):
        """Return ``(status, payload)``; runs the root scan off the caller.

        The first call starts a background scan and returns the cached list; the
        UI polls again. Calls closer than :data:`SCAN_MIN_INTERVAL_SECONDS` reuse
        the cache instead of touching the radio.
        """
        if self._scan_fn is None:
            return 501, {'error': 'scanning is unavailable'}
        with self._scan_lock:
            fresh = (
                self._scan_cache['at'] is not None
                and self._monotonic() - self._scan_cache['at'] < SCAN_MIN_INTERVAL_SECONDS
            )
            if not fresh and not self._scan_running:
                self._scan_running = True
                threading.Thread(target=self._scan_worker, daemon=True).start()
            return 200, {
                'ok': True, 'scanning': self._scan_running,
                'networks': list(self._scan_cache['networks']),
            }

    def _scan_worker(self):
        try:
            found = self._scan_fn()
        except Exception:  # noqa: BLE001 - a scan must never crash the service
            found = None
        with self._scan_lock:
            if isinstance(found, list):
                self._scan_cache['networks'] = found[:64]
            self._scan_cache['at'] = self._monotonic()
            self._scan_running = False

    # -- persistence helpers -------------------------------------------- #

    def _load_device(self):
        try:
            return config_schema.load_device(self._device_path)
        except config_schema.ConfigError:
            return {}

    def _load_secrets(self):
        try:
            return config_schema.load_secrets(self._secrets_path)
        except config_schema.ConfigError:
            return {}

    def _update_device(self, mutate):
        try:
            device = config_schema.load_device(self._device_path)
            secrets_cfg = config_schema.load_secrets(self._secrets_path)
            mutate(device)
            return bool(config_schema.save_pair(
                device, secrets_cfg, self._device_path, self._secrets_path))
        except config_schema.ConfigError:
            return False


def _is_unknown_verb(result):
    """An old image's helper exits 2 for a verb it does not know."""
    return 'exit 2' in (getattr(result, 'reason', '') or '')


def parse_scan_output(text):
    """Parse ``nmcli -t -f SSID,SIGNAL,SECURITY`` into a de-duplicated list.

    Hidden networks (empty SSID) are dropped; for duplicate SSIDs the strongest
    signal wins. Sorted strongest first.
    """
    best = {}
    for line in (text or '').splitlines():
        parts = re.split(r'(?<!\\):', line)
        if len(parts) < 3:
            continue
        ssid = parts[0].replace('\\:', ':').replace('\\\\', '\\')
        if not ssid or len(ssid.encode('utf-8')) > MAX_SSID_BYTES:
            continue
        try:
            signal = int(parts[1])
        except ValueError:
            signal = 0
        security = parts[2].strip()
        entry = {'ssid': ssid, 'signal': signal, 'secured': bool(security and security != '--')}
        if ssid not in best or signal > best[ssid]['signal']:
            best[ssid] = entry
    return sorted(best.values(), key=lambda item: -item['signal'])
