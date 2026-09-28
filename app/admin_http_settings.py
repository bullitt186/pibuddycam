"""Settings mutations and the Prusa/MQTT integration configuration (WP-UI3/WP-UI4).

Handler mixin for :class:`admin_http.AdminApp`. The route table, the security
policy and the shared helpers stay in ``admin_http``; this module only holds the
handlers of one area so ``admin_http`` stays reviewable. It is imported by
``admin_http`` after those helpers exist, so it must not be imported on its own.
"""
import logging

import config_schema
import settings_coordinator

from admin_http import (  # noqa: E402 - partially initialised, see above
    FINGERPRINT_BINDING_WARNING,
    INTEGRATION_RESTART_WARNING,
    MAX_INTEGRATION_FIELD_CHARS,
    MAX_SETTINGS_BODY_BYTES,
    TRUSTED_LAN_INTERFACES,
    _NULLABLE_SETTINGS,
    _body_size,
    _bounded_reason,
    _integration_size_error,
    _integration_strings,
    _mqtt_integration_view,
    _prusa_integration_view,
    lan_warning,
)

log = logging.getLogger('pibuddycam.admin_http')


class SettingsHandlers:
    """Handlers for this area; ``self`` is the ``AdminApp``."""

    def _handle_settings_patch(self, request, match, body_data, now):
        """Apply one bounded settings mutation through the live coordinator.

        Authenticated + CSRF (enforced by ``_authorize`` for PATCH). The admin
        process never writes ``state.json``: it forwards the field and value to
        the camera runtime, whose single ``SettingsCoordinator`` validates,
        applies, persists, and returns the authoritative snapshot. Unknown
        fields, type confusion, and oversize values are rejected before they
        cross the socket; a coordinator rejection returns the unchanged
        snapshot plus a bounded, non-secret reason (AC-5/AC-6).
        """
        if _body_size(request) > MAX_SETTINGS_BODY_BYTES:
            return self._error(request, 413, 'request body too large')
        if not isinstance(body_data, dict):
            return self._error(request, 400, 'invalid request body')
        unknown = [key for key in body_data if key not in ('field', 'value')]
        if unknown:
            return self._error(request, 400, 'unknown field')
        field = body_data.get('field')
        if not isinstance(field, str) or field not in settings_coordinator.MUTATION_FIELDS:
            return self._error(request, 400, 'unknown setting field')
        if 'value' not in body_data:
            return self._error(request, 400, 'value is required')
        value = body_data.get('value')
        if isinstance(value, str) and len(value) > MAX_INTEGRATION_FIELD_CHARS:
            return self._error(request, 400, 'value is too long')
        if value is None and field in _NULLABLE_SETTINGS:
            pass  # e.g. clearing a GPIO pin
        elif isinstance(value, float) or not isinstance(value, (str, int, bool)):
            return self._error(request, 400, 'value has an unsupported type')

        if self._settings_actions is None:
            return self._json(request, 503, {
                'ok': False, 'error': 'runtime unavailable', 'runtime': 'unavailable',
            })
        try:
            result = self._settings_actions(field, value)
        except Exception:  # noqa: BLE001 - an action must never crash routing
            log.warning('admin_http: settings action failed')
            result = None
        if not isinstance(result, dict):
            return self._json(request, 503, {
                'ok': False, 'error': 'runtime unavailable', 'runtime': 'unavailable',
            })
        if not result.get('ok') and result.get('degraded'):
            return self._json(request, 503, {
                'ok': False,
                'error': _bounded_reason(result.get('error')) or 'runtime unavailable',
                'runtime': 'unavailable',
            })
        payload = {'ok': bool(result.get('ok')), 'field': field}
        settings = result.get('settings')
        if isinstance(settings, dict):
            payload['settings'] = settings
        if result.get('ok'):
            payload['changed'] = list(result.get('changed') or [])
        else:
            payload['error'] = _bounded_reason(result.get('error')) or 'setting rejected'
        payload['runtime'] = 'live'
        return self._json(request, 200, payload)

    def _handle_integrations_get(self, request, match, body_data, now):
        """Return the redacted Prusa/MQTT configuration and runtime state.

        Never returns a stored token, password, PSK, or MQTT URI userinfo: the
        documents are projected onto configured-state booleans and a userinfo-free
        URI, and the runtime observations come from the already-redacted
        dashboard provider.
        """
        device = self._load_device_safe()
        secrets = self._load_secrets_safe()
        return self._json(request, 200, {
            'ok': True,
            'prusa': _prusa_integration_view(device, secrets),
            'mqtt': _mqtt_integration_view(device, secrets),
            'runtime': self._integration_runtime_view(),
            'trusted_lan': {
                'notice': lan_warning(),
                'interfaces': TRUSTED_LAN_INTERFACES,
            },
        })

    def _integration_runtime_view(self):
        """Return the runtime Prusa/MQTT observations, or an honest unavailable."""
        provider = self._dashboard_provider
        payload = None
        if provider is not None:
            try:
                payload = provider()
            except Exception:  # noqa: BLE001 - a provider must never crash routing
                payload = None
        if not isinstance(payload, dict):
            return {'source': 'unavailable', 'fresh': False, 'prusa': {}, 'mqtt': {}}
        runtime = payload.get('runtime') if isinstance(payload.get('runtime'), dict) else {}
        prusa = payload.get('prusa') if isinstance(payload.get('prusa'), dict) else {}
        mqtt = payload.get('mqtt') if isinstance(payload.get('mqtt'), dict) else {}
        return {
            'source': _bounded_reason(runtime.get('source')) or 'unknown',
            'fresh': bool(runtime.get('fresh')),
            'prusa': prusa,
            'mqtt': mqtt,
        }

    def _handle_integrations_mqtt_put(self, request, match, body_data, now):
        """Validate and atomically persist the MQTT configuration (AC-8).

        Re-auth + CSRF (route policy). A blank ``username``/``password`` keeps
        the stored value; an explicit ``clear_username``/``clear_password`` flag
        removes it. Unknown fields, wrong types, userinfo in the URI, and
        oversize values are rejected before any write, so a failed validation
        leaves both files byte-for-byte unchanged. The response is truthful
        about activation: the saved change is pending a runtime restart.
        """
        size_error = _integration_size_error(request)
        if size_error is not None:
            return size_error
        if not isinstance(body_data, dict):
            return self._error(request, 400, 'invalid request body')
        allowed = {
            'enabled', 'uri', 'client_id', 'discovery_prefix', 'topic_prefix',
            'ca_file', 'username', 'password', 'clear_username', 'clear_password',
        }
        unknown = [key for key in body_data if key not in allowed]
        if unknown:
            return self._error(request, 400, 'unknown field')
        for name in ('enabled', 'clear_username', 'clear_password'):
            if name in body_data and type(body_data[name]) is not bool:
                return self._error(request, 400, f'{name} must be a boolean')
        strings, error = _integration_strings(
            body_data,
            ('uri', 'client_id', 'discovery_prefix', 'topic_prefix', 'ca_file',
             'username', 'password'),
        )
        if error is not None:
            return error

        try:
            device_cfg = config_schema.load_device(self._device_path)
            secrets_cfg = config_schema.load_secrets(self._secrets_path)
        except config_schema.ConfigError:
            return self._error(
                request, 409, 'existing configuration is invalid; use the expert editor')

        mqtt = device_cfg.setdefault('mqtt', {})
        if 'enabled' in body_data:
            mqtt['enabled'] = body_data['enabled']
        for name in ('uri', 'client_id', 'discovery_prefix', 'topic_prefix', 'ca_file'):
            if name in body_data:
                mqtt[name] = strings[name]

        secret_mqtt = secrets_cfg.get('mqtt')
        if not isinstance(secret_mqtt, dict):
            secret_mqtt = {}
        if body_data.get('clear_username'):
            secret_mqtt.pop('username', None)
        elif strings['username']:
            secret_mqtt['username'] = strings['username']
        if body_data.get('clear_password'):
            secret_mqtt.pop('password', None)
        elif strings['password']:
            secret_mqtt['password'] = strings['password']
        if secret_mqtt:
            secrets_cfg['mqtt'] = secret_mqtt
        else:
            secrets_cfg.pop('mqtt', None)

        submitted = tuple(
            value for value in (
                strings['username'], strings['password'], strings['uri'])
            if value)
        try:
            saved = config_schema.save_pair(
                device_cfg, secrets_cfg, self._device_path, self._secrets_path)
        except config_schema.ConfigError as e:
            return self._json(request, 400, {
                'ok': False,
                'error': _bounded_reason(e) or 'invalid MQTT configuration',
            }, secrets=submitted)
        if not saved:
            return self._json(request, 500, {
                'ok': False, 'error': 'could not save MQTT configuration',
            }, secrets=submitted)
        return self._json(request, 200, {
            'ok': True,
            'saved': True,
            'active': False,
            'restart_required': True,
            'warnings': [INTEGRATION_RESTART_WARNING],
            'mqtt': _mqtt_integration_view(device_cfg, secrets_cfg),
        }, secrets=submitted)

    def _handle_integrations_prusa_put(self, request, match, body_data, now):
        """Replace the Prusa server/token/fingerprint safely (AC-9).

        Re-auth + CSRF. A blank ``token``/``fingerprint`` keeps the stored
        value; ``clear_token``/``clear_fingerprint`` remove it explicitly. The
        stored token is never returned. The response warns that a fingerprint
        replacement can invalidate the token binding and that the change is
        pending a runtime restart.
        """
        size_error = _integration_size_error(request)
        if size_error is not None:
            return size_error
        if not isinstance(body_data, dict):
            return self._error(request, 400, 'invalid request body')
        allowed = {'server', 'token', 'fingerprint', 'clear_token', 'clear_fingerprint'}
        unknown = [key for key in body_data if key not in allowed]
        if unknown:
            return self._error(request, 400, 'unknown field')
        for name in ('clear_token', 'clear_fingerprint'):
            if name in body_data and type(body_data[name]) is not bool:
                return self._error(request, 400, f'{name} must be a boolean')
        strings, error = _integration_strings(
            body_data, ('server', 'token', 'fingerprint'))
        if error is not None:
            return error

        try:
            device_cfg = config_schema.load_device(self._device_path)
            secrets_cfg = config_schema.load_secrets(self._secrets_path)
        except config_schema.ConfigError:
            return self._error(
                request, 409, 'existing configuration is invalid; use the expert editor')

        if 'server' in body_data:
            server = strings['server'].strip()
            if not server:
                return self._error(request, 400, 'prusa server must not be empty')
            device_cfg.setdefault('prusa', {})['server'] = server

        fingerprint_changed = False
        if body_data.get('clear_fingerprint'):
            device_cfg['fingerprint'] = ''
            fingerprint_changed = True
        elif strings['fingerprint'].strip():
            device_cfg['fingerprint'] = strings['fingerprint'].strip()
            fingerprint_changed = True

        if body_data.get('clear_token'):
            secrets_cfg.pop('prusa', None)
        elif strings['token']:
            secrets_cfg.setdefault('prusa', {})['token'] = strings['token']

        submitted = tuple(
            value for value in (strings['token'], strings['fingerprint']) if value)
        try:
            saved = config_schema.save_pair(
                device_cfg, secrets_cfg, self._device_path, self._secrets_path)
        except config_schema.ConfigError as e:
            return self._json(request, 400, {
                'ok': False,
                'error': _bounded_reason(e) or 'invalid Prusa configuration',
            }, secrets=submitted)
        if not saved:
            return self._json(request, 500, {
                'ok': False, 'error': 'could not save Prusa configuration',
            }, secrets=submitted)
        warnings = [INTEGRATION_RESTART_WARNING]
        if fingerprint_changed:
            warnings.insert(0, FINGERPRINT_BINDING_WARNING)
        return self._json(request, 200, {
            'ok': True,
            'saved': True,
            'active': False,
            'restart_required': True,
            'warnings': warnings,
            'prusa': _prusa_integration_view(device_cfg, secrets_cfg),
        }, secrets=submitted)
