import asyncio
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

# GAP-AUTH-01 flow tests exercise signaling.PrusaSignaling._authenticate, whose
# module imports socketio. Inject a minimal in-process double so the real runtime
# dependency is never loaded (tests must not import socketio).
_socketio_stub = types.ModuleType('socketio')
_socketio_stub.AsyncClient = object
sys.modules['socketio'] = _socketio_stub

from auth import auth_ack_is_success  # noqa: E402
import signaling  # noqa: E402


class AuthAckPredicateTests(unittest.TestCase):
    def test_exact_integer_zero_succeeds(self):
        self.assertTrue(auth_ack_is_success(0))

    def test_all_other_values_fail(self):
        # 1/5: other ints; False (== 0) must not pass via bool; '0': string;
        # None; malformed payloads (bytes/list/dict) and non-int numerics.
        for ack in (1, 5, -1, True, False, '0', 'false', None, 0.0, b'\x00', [], {}):
            with self.subTest(ack=ack):
                self.assertFalse(auth_ack_is_success(ack))


class _FakeSio:
    def __init__(self, ack=None, error=None):
        self._ack = ack
        self._error = error
        self.calls = []
        self.disconnects = 0

    async def call(self, event, data, timeout=None):
        self.calls.append((event, data, timeout))
        if self._error is not None:
            raise self._error
        return self._ack

    async def disconnect(self):
        self.disconnects += 1


class _FakeSignaling:
    def __init__(self, sio):
        self.sio = sio
        self.fingerprint = 'fingerprint-value'
        self.token = 'token-value'
        self.post_auth_count = 0
        self._authenticated = False

    async def _send_post_auth(self):
        self.post_auth_count += 1

class AuthenticateFlowTests(unittest.TestCase):
    def _authenticate(self, sio):
        sig = _FakeSignaling(sio)
        accepted = asyncio.run(signaling.PrusaSignaling._authenticate(sig))
        return sig, accepted

    def test_ack_zero_proceeds_to_post_auth(self):
        sig, accepted = self._authenticate(_FakeSio(ack=0))
        self.assertTrue(accepted)
        self.assertTrue(sig._authenticated)
        self.assertEqual(sig.post_auth_count, 1)
        self.assertEqual(sig.sio.calls[0][0], 'camera_authentication')
        self.assertEqual(sig.sio.disconnects, 0)

    def test_rejected_acks_emit_no_post_auth(self):
        for ack in (1, 5, True, False, '0', None, b'', [], {}):
            with self.subTest(ack=ack):
                sig, accepted = self._authenticate(_FakeSio(ack=ack))
                self.assertFalse(accepted)
                self.assertFalse(sig._authenticated)
                self.assertEqual(sig.post_auth_count, 0)
                # Disconnecting inside python-socketio's connect callback can
                # deadlock the initial connection.  The unauthenticated flag
                # makes the supervisor replace this client instead.
                self.assertEqual(sig.sio.disconnects, 0)

    def test_timeout_emits_no_post_auth(self):
        sig, accepted = self._authenticate(
            _FakeSio(error=TimeoutError('auth timeout'))
        )
        self.assertFalse(accepted)
        self.assertFalse(sig._authenticated)
        self.assertEqual(sig.post_auth_count, 0)
        self.assertEqual(sig.sio.disconnects, 0)

    def test_exception_emits_no_post_auth(self):
        sig, accepted = self._authenticate(_FakeSio(error=RuntimeError('boom')))
        self.assertFalse(accepted)
        self.assertFalse(sig._authenticated)
        self.assertEqual(sig.post_auth_count, 0)
        self.assertEqual(sig.sio.disconnects, 0)


class InitialConnectBoundTests(unittest.TestCase):
    def test_initial_connect_timeout_returns_for_supervisor(self):
        class _HungSignaling:
            async def _connect_once(self):
                await asyncio.Event().wait()

        original = signaling.INITIAL_CONNECT_TIMEOUT_SECONDS
        signaling.INITIAL_CONNECT_TIMEOUT_SECONDS = 0.01
        try:
            # A timeout is swallowed deliberately: main must continue and start
            # the long-lived reconnect supervisor.
            asyncio.run(signaling.PrusaSignaling.connect(_HungSignaling()))
        finally:
            signaling.INITIAL_CONNECT_TIMEOUT_SECONDS = original


class SupervisedSessionHealthTests(unittest.TestCase):
    def _usable(self, connected, eio_state, authenticated):
        sig = SimpleNamespace(
            sio=SimpleNamespace(
                connected=connected,
                eio=SimpleNamespace(state=eio_state),
            ),
            _authenticated=authenticated,
        )
        return signaling.PrusaSignaling._session_is_usable(sig)

    def test_transport_without_auth_ack_is_not_usable(self):
        self.assertFalse(self._usable(True, 'connected', False))

    def test_authenticated_connected_transport_is_usable(self):
        self.assertTrue(self._usable(True, 'connected', True))

    def test_closed_transport_is_not_usable_even_after_prior_auth(self):
        self.assertFalse(self._usable(False, 'disconnected', True))


if __name__ == '__main__':
    unittest.main()
