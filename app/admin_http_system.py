"""System facts, updates, diagnostics and the reboot action (WP-UI7).

Handler mixin for :class:`admin_http.AdminApp`. The route table, the security
policy and the shared helpers stay in ``admin_http``; this module only holds the
handlers of one area so ``admin_http`` stays reviewable. It is imported by
``admin_http`` after those helpers exist, so it must not be imported on its own.
"""
import math

import device_control
import runtime_ipc
import ssh_control

from admin_http import (  # noqa: E402 - partially initialised, see above
    DISRUPTIVE_ACTION_WARNING,
    MAX_SYSTEM_ACTION_BODY_BYTES,
    REBOOT_UNAVAILABLE,
    UPDATE_INSTALL_WARNING,
    UPDATE_UNAVAILABLE,
    _body_size,
    _bounded_hostname,
    _bounded_reason,
    _system_action_body_error,
)


class SystemHandlers:
    """Handlers for this area; ``self`` is the ``AdminApp``."""

    # ------------------------------------------------------------------ #
    # System, updates, diagnostics, and destructive actions (WP-UI7)
    # ------------------------------------------------------------------ #

    def _handle_system(self, request, match, body_data, now):
        """Authenticated system facts (WP-UI7; AC-14/AC-17).

        Reports the active release identity, provisioning state, SSH state, and
        the local hostname from bounded sources. It exposes no secret, personal
        path, stored credential, or browser-supplied value. Health, storage,
        temperature, and uptime come from the already-authenticated
        ``GET /api/dashboard`` so there is one authoritative runtime source.
        """
        view = self._provisioning_view()
        payload = {
            'ok': True,
            'generated_at': now,
            'version': self._version_identity_view(),
            'provisioning': {
                'state': view.state,
                'source': view.source,
                'setup_available': view.setup_available,
            },
            'ssh': self._ssh_status_view(now),
            'network': {'hostname': _bounded_hostname(self._hostname_fn)},
        }
        if self._network is not None:
            payload['network']['address'] = self._network.current_address()
        if view.error:
            payload['provisioning']['error'] = view.error
        return self._json(request, 200, payload)

    def _version_identity_view(self):
        """Return the bounded active release/source identity (never a path)."""
        try:
            identity = self._release_identity_fn()
        except Exception:  # noqa: BLE001 - identity must never crash a request
            identity = {}
        if not isinstance(identity, dict):
            identity = {}
        try:
            application = self._application_version_fn()
        except Exception:  # noqa: BLE001
            application = ''
        return {
            'application': runtime_ipc.bounded_text(application, 128),
            'release': runtime_ipc.bounded_text(identity.get('version'), 128),
            'source_commit': runtime_ipc.bounded_text(identity.get('source_commit'), 64),
            'os_suite': runtime_ipc.bounded_text(identity.get('os_suite'), 64),
            'kernel': runtime_ipc.bounded_text(identity.get('kernel_package'), 64),
        }

    def _ssh_status_view(self, now):
        """Return SSH enabled state with freshness (never raises)."""
        if self._ssh_runner is None:
            return {'ok': False, 'enabled': None, 'observed_at': None, 'fresh': False}
        try:
            result = ssh_control.ssh_enabled(self._ssh_runner)
        except Exception:  # noqa: BLE001 - SSH state must never crash a request
            return {'ok': False, 'enabled': None, 'observed_at': None, 'fresh': False}
        return {
            'ok': bool(result.ok),
            'enabled': bool(result.enabled),
            'observed_at': now if result.ok else None,
            'fresh': bool(result.ok),
        }

    def _handle_update(self, request, match, body_data, now):
        """Return the bounded signed-update state (WP-UI7; AC-15).

        The projection never includes the manifest/bundle URL, signing key,
        channel, or command, and no browser input is read.
        """
        manager = self._update_manager
        if manager is None:
            return self._json(request, 200, {
                'ok': True, 'available': False, 'state': 'unknown',
                'reason': UPDATE_UNAVAILABLE,
            })
        try:
            view = manager.state()
        except Exception:  # noqa: BLE001 - the view must never crash a request
            view = None
        if not isinstance(view, dict):
            return self._json(request, 200, {
                'ok': True, 'available': False, 'state': 'unknown',
                'reason': UPDATE_UNAVAILABLE,
            })
        payload = dict(view)
        payload['ok'] = True
        payload['available'] = True
        return self._json(request, 200, payload)

    def _handle_update_check(self, request, match, body_data, now):
        """Start one report-only update check (WP-UI7; AC-15).

        Authenticated + CSRF. The body must be empty: the manifest URL, signing
        key, channel, version, service name, and command are all fixed in the
        root updater path and can never be supplied by the browser. This never
        installs anything.
        """
        body_error = _system_action_body_error(request, body_data)
        if body_error is not None:
            return body_error
        manager = self._update_manager
        if manager is None:
            return self._json(request, 503, {'ok': False, 'error': UPDATE_UNAVAILABLE})
        try:
            result = manager.check()
        except Exception:  # noqa: BLE001 - an action must never crash routing
            result = None
        if not isinstance(result, dict):
            return self._json(request, 503, {'ok': False, 'error': UPDATE_UNAVAILABLE})
        if result.get('busy'):
            return self._json(request, 409, {
                'ok': False,
                'error': _bounded_reason(result.get('reason')) or 'update check already in progress',
            })
        return self._json(request, 202, {
            'ok': True, 'started': True, 'checking': True,
            'warning': 'Update check started. This is report-only and never installs.',
        })

    def _handle_update_install(self, request, match, body_data, now):
        """Start one fixed-privileged signed update install (WP-UI7; AC-15).

        Re-auth + CSRF (route policy). The body may carry the inline re-auth
        password (which the policy already consumed) but nothing else: no URL,
        CA, key, channel, bundle, manifest, version, service name, or command is
        accepted. The install runs through the fixed privileged helper and may
        restart the admin service, terminating this request; the response says so
        and the UI must reconnect rather than claim completion.
        """
        body_error = _system_action_body_error(request, body_data, allow_password=True)
        if body_error is not None:
            return body_error
        manager = self._update_manager
        if manager is None:
            return self._json(request, 503, {'ok': False, 'error': UPDATE_UNAVAILABLE})
        try:
            result = manager.install()
        except Exception:  # noqa: BLE001
            result = None
        if not isinstance(result, dict):
            return self._json(request, 503, {'ok': False, 'error': UPDATE_UNAVAILABLE})
        if result.get('busy'):
            return self._json(request, 409, {
                'ok': False,
                'error': _bounded_reason(result.get('reason')) or 'update install already in progress',
            })
        return self._json(request, 202, {
            'ok': True, 'started': True, 'installing': True,
            'warning': UPDATE_INSTALL_WARNING,
        })

    def _handle_diagnostics(self, request, match, body_data, now):
        """Return bounded, redacted current-boot diagnostics (WP-UI7; AC-17).

        The provider owns the fixed command set and the byte/line/time bounds;
        the request cannot supply a unit, path, line count, or command. The
        provider collects on a background thread, so this handler never blocks
        the core worker on ``journalctl``.
        """
        provider = self._diagnostics_provider
        if provider is None:
            return self._json(request, 200, {
                'ok': True, 'available': False, 'state': 'unavailable',
                'reason': 'diagnostics unavailable',
            })
        try:
            document = provider(secrets=self._known_secrets())
        except Exception:  # noqa: BLE001 - diagnostics must never crash a request
            document = None
        if not isinstance(document, dict):
            return self._json(request, 200, {
                'ok': True, 'available': False, 'state': 'unavailable',
                'reason': 'diagnostics unavailable',
            })
        payload = dict(document)
        payload['ok'] = True
        return self._json(request, 200, payload)

    def _handle_reboot(self, request, match, body_data, now):
        """Reboot through the fixed-privileged verb, rate-limited (AC-16).

        Fresh-window-only re-auth + CSRF (route policy) and an explicit
        ``confirm: true`` flag. The existing 60 s :mod:`device_control` rate
        limit is preserved; a second request inside the window is a bounded
        ``429`` with ``Retry-After``. The only command issued is the fixed
        privileged ``reboot`` verb.
        """
        if _body_size(request) > MAX_SYSTEM_ACTION_BODY_BYTES:
            return self._error(request, 413, 'request body too large')
        if not isinstance(body_data, dict):
            return self._error(request, 400, 'invalid request body')
        unknown = [key for key in body_data if key not in ('confirm',)]
        if unknown:
            return self._error(request, 400, 'unknown field')
        if body_data.get('confirm') is not True:
            return self._json(request, 400, {
                'ok': False, 'error': 'explicit confirmation is required',
            })
        if self._reboot_fn is None:
            return self._json(request, 503, {'ok': False, 'error': REBOOT_UNAVAILABLE})
        last = self._reboot_state.last_reboot_monotonic
        interval = device_control.DEFAULT_REBOOT_MIN_INTERVAL_SECONDS
        if not device_control.can_reboot(last, now, interval):
            elapsed = now - last if last is not None else 0.0
            remaining = max(0.0, interval - elapsed)
            response = self._json(request, 429, {
                'ok': False, 'error': 'reboot rate limit is active',
            })
            response.headers['Retry-After'] = str(max(1, int(math.ceil(remaining))))
            return response
        accepted = device_control.request_reboot(
            self._reboot_state, self._reboot_fn, now=now)
        if not accepted:
            return self._json(request, 500, {
                'ok': False, 'error': 'reboot command failed',
            })
        return self._json(request, 200, {
            'ok': True, 'accepted': True, 'warning': DISRUPTIVE_ACTION_WARNING,
        })
