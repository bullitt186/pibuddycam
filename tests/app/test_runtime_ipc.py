"""WP-UI2 (AC-4/AC-17/AC-18): bounded local runtime-control IPC.

Stdlib-only and hermetic: every server binds a ``tempfile`` Unix socket and
every client talks to that path. No real ``/run/pibuddycam``, no device, and no
network are touched.
"""
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import runtime_ipc  # noqa: E402


class _ServerHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.socket_path = os.path.join(self._tmp.name, 'control.sock')
        self.server = runtime_ipc.RuntimeServer(
            self.socket_path,
            handlers={'dashboard': lambda params: {'seen': params}},
            timeout=1.0,
        )
        self.addCleanup(self.server.stop)
        self.server.start()
        self.client = runtime_ipc.RuntimeClient(self.socket_path, timeout=1.0)

    def raw_connect(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(2.0)
        connection.connect(self.socket_path)
        return connection


class SocketLifecycleTests(_ServerHarness):
    def test_socket_is_created_with_the_service_mode(self):
        self.assertTrue(os.path.exists(self.socket_path))
        mode = os.stat(self.socket_path).st_mode & 0o777
        self.assertEqual(mode, runtime_ipc.DEFAULT_SOCKET_MODE)

    def test_ping_round_trips(self):
        result = self.client.ping()
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['data']['pong'])

    def test_dashboard_handler_receives_bounded_params(self):
        result = self.client.dashboard()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['data']['seen'], {})

    def test_stop_unlinks_the_socket(self):
        self.server.stop()
        self.assertFalse(os.path.lexists(self.socket_path))

    def test_start_refuses_to_replace_a_non_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'control.sock')
            Path(path).write_text('not a socket', encoding='utf-8')
            server = runtime_ipc.RuntimeServer(path, handlers={})
            with self.assertRaises(OSError):
                server.start()
            # The real file is left untouched.
            self.assertEqual(Path(path).read_text(encoding='utf-8'), 'not a socket')

    def test_start_is_idempotent(self):
        self.server.start()
        self.assertTrue(self.server.started)

    def test_stale_socket_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'control.sock')
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(path)
            stale.close()
            server = runtime_ipc.RuntimeServer(path, handlers={})
            self.addCleanup(server.stop)
            server.start()
            self.assertTrue(server.started)


class OperationAllowlistTests(_ServerHarness):
    def test_unknown_operation_is_rejected(self):
        result = self.client.request('evil')
        self.assertFalse(result['ok'])
        self.assertIn('not allowed', result['error'])

    def test_client_rejects_a_non_allowlisted_operation_before_connecting(self):
        # Even with the server down, an unknown op is refused locally.
        client = runtime_ipc.RuntimeClient(
            os.path.join(self._tmp.name, 'missing.sock'))
        result = client.request('delete_everything')
        self.assertFalse(result['ok'])
        self.assertIn('not allowed', result['error'])

    def test_handler_table_only_accepts_allowlisted_names(self):
        server = runtime_ipc.RuntimeServer(
            os.path.join(self._tmp.name, 'other.sock'),
            handlers={'evil': lambda params: {'pwned': True}},
        )
        self.assertNotIn('evil', server._handlers)
        self.assertIn('ping', server._handlers)


class FramingTests(_ServerHarness):
    def test_malformed_json_gets_a_bounded_error(self):
        connection = self.raw_connect()
        self.addCleanup(connection.close)
        connection.sendall(b'{not json\n')
        response = connection.recv(4096)
        payload = json.loads(response.decode('utf-8'))
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['error'], 'malformed request')

    def test_non_object_request_is_rejected(self):
        connection = self.raw_connect()
        self.addCleanup(connection.close)
        connection.sendall(b'["dashboard"]\n')
        payload = json.loads(connection.recv(4096).decode('utf-8'))
        self.assertFalse(payload['ok'])

    def test_invalid_params_type_is_rejected(self):
        connection = self.raw_connect()
        self.addCleanup(connection.close)
        connection.sendall(b'{"op": "dashboard", "params": [1, 2]}\n')
        payload = json.loads(connection.recv(4096).decode('utf-8'))
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['error'], 'invalid params')

    def test_oversized_request_line_is_dropped(self):
        connection = self.raw_connect()
        self.addCleanup(connection.close)
        blob = b'{"op": "dashboard", "params": {"x": "' + b'a' * (70 * 1024) + b'"}}\n'
        connection.sendall(blob)
        try:
            response = connection.recv(4096)
        except OSError:
            response = b''
        # No response: the server refuses to parse an over-limit line.
        self.assertEqual(response, b'')

    def test_response_size_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'control.sock')
            server = runtime_ipc.RuntimeServer(
                path,
                handlers={'dashboard': lambda params: {'blob': 'x' * 4096}},
                max_response_bytes=512,
                timeout=1.0,
            )
            self.addCleanup(server.stop)
            server.start()
            client = runtime_ipc.RuntimeClient(path, timeout=1.0)
            result = client.request('dashboard')
            self.assertFalse(result['ok'])
            self.assertIn('too large', result['error'])

    def test_handler_exception_is_isolated(self):
        def boom(params):
            raise RuntimeError('nope')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'control.sock')
            server = runtime_ipc.RuntimeServer(
                path, handlers={'dashboard': boom}, timeout=1.0)
            self.addCleanup(server.stop)
            server.start()
            client = runtime_ipc.RuntimeClient(path, timeout=1.0)
            result = client.dashboard()
            self.assertFalse(result['ok'])
            self.assertEqual(result['error'], 'operation failed')

    def test_no_traceback_or_raw_exception_is_returned(self):
        def boom(params):
            raise ValueError('secret-traceback-canary')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'control.sock')
            server = runtime_ipc.RuntimeServer(
                path, handlers={'dashboard': boom}, timeout=1.0)
            self.addCleanup(server.stop)
            server.start()
            client = runtime_ipc.RuntimeClient(path, timeout=1.0)
            body = json.dumps(client.dashboard())
            self.assertNotIn('secret-traceback-canary', body)
            self.assertNotIn('Traceback', body)


class TimeoutAndAdmissionTests(_ServerHarness):
    def test_slow_client_does_not_starve_a_fast_client(self):
        slow = self.raw_connect()
        self.addCleanup(slow.close)
        # The slow connection sends nothing and holds one worker; the fast
        # client must still be served by the second worker.
        result = self.client.ping()
        self.assertTrue(result['ok'], result)

    def test_connection_cap_drops_excess_peers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'control.sock')
            server = runtime_ipc.RuntimeServer(
                path,
                handlers={'dashboard': lambda params: {'ok': True}},
                timeout=1.0,
                max_workers=1,
                max_connections=1,
            )
            self.addCleanup(server.stop)
            server.start()
            slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            slow.settimeout(2.0)
            slow.connect(path)
            self.addCleanup(slow.close)
            time.sleep(0.1)  # let the accept loop admit the slow peer
            client = runtime_ipc.RuntimeClient(path, timeout=1.0)
            result = client.ping()
            self.assertFalse(result['ok'])

    def test_client_to_missing_socket_is_degraded(self):
        client = runtime_ipc.RuntimeClient(
            os.path.join(self._tmp.name, 'missing.sock'), timeout=0.5)
        result = client.dashboard()
        self.assertFalse(result['ok'])
        self.assertTrue(result['degraded'])
        self.assertIn('unavailable', result['error'])


class BoundingTests(unittest.TestCase):
    def test_string_is_bounded_and_printable(self):
        self.assertEqual(
            runtime_ipc.bounded_text('a\x00b' + 'x' * 100, 5), 'abxxx')

    def test_bound_value_limits_depth_items_and_text(self):
        value = {'a': list(range(10)), 'b': {'c': {'d': {'e': 1}}}}
        bounded = runtime_ipc.bound_value(
            value, depth=2, max_items=2, max_text=4)
        self.assertEqual(bounded['a'], [0, 1])
        # The depth cut leaves the nested leaf as None rather than recursing.
        self.assertEqual(bounded['b'], {'c': {'d': None}})

    def test_bound_value_stringifies_unknown_objects(self):
        bounded = runtime_ipc.bound_value(object())
        self.assertIsInstance(bounded, str)

    def test_bound_value_caps_items(self):
        bounded = runtime_ipc.bound_value(list(range(1000)), max_items=5)
        self.assertEqual(bounded, [0, 1, 2, 3, 4])


if __name__ == '__main__':
    unittest.main()
