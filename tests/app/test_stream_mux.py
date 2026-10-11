"""stream_mux: one client disconnecting must not cut off another client's stream."""
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import stream_mux  # noqa: E402


class _DyingConn:
    """A client whose first send blocks until released, then fails (peer gone)."""

    def __init__(self):
        self.release = threading.Event()

    def sendall(self, data):
        self.release.wait(5)
        raise BrokenPipeError

    def close(self):
        pass


class ChannelDisconnectTests(unittest.TestCase):
    def _channel(self):
        ch = stream_mux.Channel.__new__(stream_mux.Channel)  # no listening socket
        ch.port, ch.patch, ch.clients, ch.keyframe = 0, False, [], b''
        ch._lock = threading.Lock()
        return ch

    def _wait(self, cond):
        deadline = time.monotonic() + 5
        while not cond():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

    def test_disconnect_with_both_queues_empty_keeps_the_other_client(self):
        ch = self._channel()
        live, live_peer = socket.socketpair()
        live_peer.settimeout(2)
        self.addCleanup(live_peer.close)
        threading.Thread(target=ch._handle, args=(live,), daemon=True).start()
        self._wait(lambda: len(ch.clients) == 1)
        dying = _DyingConn()
        dying_thread = threading.Thread(target=ch._handle, args=(dying,), daemon=True)
        dying_thread.start()
        self._wait(lambda: len(ch.clients) == 2)

        ch.broadcast(b'one')
        self.assertEqual(live_peer.recv(16), b'one')  # live queue drained -> empty
        self._wait(lambda: not ch.clients[1])         # dying queue popped -> empty
        dying.release.set()                           # snapshot client goes away
        dying_thread.join(5)

        self.assertEqual(len(ch.clients), 1)
        ch.broadcast(b'two')
        self.assertEqual(live_peer.recv(16), b'two')


if __name__ == '__main__':
    unittest.main()
