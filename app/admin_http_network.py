"""Network settings, NTP servers and the GPIO pin table (Pi-only console features).

Handler mixin for :class:`admin_http.AdminApp`. The route table, the security
policy and the shared helpers stay in ``admin_http``; this module only holds the
handlers of one area so ``admin_http`` stays reviewable. It is imported by
``admin_http`` after those helpers exist, so it must not be imported on its own.
"""
import logging

import gpio_pins
import network_settings
import timezone

from admin_http import (  # noqa: E402 - partially initialised, see above
    NETWORK_APPLY_WARNING,
    _integration_size_error,
)

log = logging.getLogger('pibuddycam.admin_http')


class NetworkHandlers:
    """Handlers for this area; ``self`` is the ``AdminApp``."""

    # ------------------------------------------------------------------ #
    # Network settings, NTP and GPIO pins (Pi-only console features)
    # ------------------------------------------------------------------ #

    def _handle_network_get(self, request, match, body_data, now):
        """Live network/time status and the last apply result (never a secret)."""
        if self._network is None:
            return self._error(request, 501, 'network settings unavailable')
        try:
            status = self._network.status()
        except Exception:  # noqa: BLE001 - a status read must never crash routing
            log.warning('admin_http: network status failed')
            return self._error(request, 503, 'network status unavailable')
        payload = {'ok': True}
        payload.update(status)
        return self._json(request, 200, payload)

    def _handle_network_scan(self, request, match, body_data, now):
        """Cached Wi-Fi scan; a background thread refreshes it (rate limited)."""
        if self._network is None:
            return self._error(request, 501, 'network settings unavailable')
        status, payload = self._network.scan()
        return self._json(request, status, dict({'ok': status == 200}, **payload))

    def _network_body(self, request, body_data, allowed):
        """Shared size/shape gate; returns an error response or ``None``."""
        if self._network is None:
            return self._error(request, 501, 'network settings unavailable')
        size_error = _integration_size_error(request)
        if size_error is not None:
            return size_error
        if not isinstance(body_data, dict):
            return self._error(request, 400, 'invalid request body')
        if any(key not in allowed for key in body_data):
            return self._error(request, 400, 'unknown field')
        return None

    def _handle_network_put(self, request, match, body_data, now):
        """Validate a Wi-Fi/IPv4 change and start the root transaction (202).

        Fresh re-auth window + CSRF + explicit ``confirm: true``. The new PSK is
        never echoed, logged or placed in argv: it travels to the root unit on
        stdin only. The response is *accepted*, not applied: the device may
        change address, so the UI polls ``GET /api/network`` and shows where to
        reconnect. The unit reverts to the old profile when the new one does not
        come up.
        """
        allowed = {'ssid', 'psk', 'ipv4_method', 'address', 'prefix', 'gateway',
                   'dns', 'confirm'}
        error = self._network_body(request, body_data, allowed)
        if error is not None:
            return error
        psk = body_data.get('psk')
        secrets = (psk,) if isinstance(psk, str) and psk else ()
        if body_data.get('confirm') is not True:
            return self._json(request, 400, {
                'ok': False, 'error': 'explicit confirmation is required',
            }, secrets=secrets)
        try:
            checked = network_settings.validate_request(body_data)
        except network_settings.ValidationError as e:
            return self._json(request, 400, {'ok': False, 'error': str(e)}, secrets=secrets)
        status, payload = self._network.apply(checked)
        payload = dict(payload)
        payload['ok'] = status == 202
        if status == 202:
            payload['warning'] = NETWORK_APPLY_WARNING
        return self._json(request, status, payload, secrets=secrets)

    def _handle_network_hostname_put(self, request, match, body_data, now):
        """Apply and persist the hostname (fresh re-auth window + CSRF)."""
        error = self._network_body(request, body_data, {'hostname'})
        if error is not None:
            return error
        try:
            name = network_settings.validate_hostname(body_data.get('hostname'))
        except network_settings.ValidationError as e:
            return self._json(request, 400, {'ok': False, 'error': str(e)})
        status, payload = self._network.set_hostname(name)
        payload = dict(payload)
        payload['ok'] = status == 200
        return self._json(request, status, payload)

    def _handle_network_ntp_put(self, request, match, body_data, now):
        """Save the NTP servers (blank = DHCP, then pool) and apply them."""
        error = self._network_body(request, body_data, {'ntp_servers'})
        if error is not None:
            return error
        try:
            servers = network_settings.validate_ntp_servers(body_data.get('ntp_servers', []))
        except network_settings.ValidationError as e:
            return self._json(request, 400, {'ok': False, 'error': str(e)})
        status, payload = self._network.set_ntp_servers(servers)
        payload = dict(payload)
        payload['ok'] = status == 200
        return self._json(request, status, payload)

    def _handle_timezones(self, request, match, body_data, now):
        """The IANA zone names this device can be set to (fetched once by the UI)."""
        return self._json(request, 200, {'ok': True, 'zones': timezone.available_zones()})

    def _handle_gpio_pins(self, request, match, body_data, now):
        """Safe GPIO pin table plus the runtime trigger status."""
        status = None
        provider = self._dashboard_provider
        if provider is not None:
            try:
                document = provider()
            except Exception:  # noqa: BLE001 - a provider must never crash routing
                document = None
            if isinstance(document, dict) and isinstance(
                    document.get('timelapse_gpio'), dict):
                status = document['timelapse_gpio']
        return self._json(request, 200, {
            'ok': True,
            'pins': gpio_pins.pin_table(),
            'defaults': {
                'shot': gpio_pins.DEFAULT_SHOT_PIN,
                'record': gpio_pins.DEFAULT_RECORD_PIN,
            },
            'status': status,
        })
