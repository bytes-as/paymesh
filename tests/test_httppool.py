"""httppool: threads share a few kept-alive connections, a dropped idle one is retried once on
a new one, and close() closes them (no socket is left to the garbage collector)."""
import http.server
import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import httppool  # noqa: E402


class Echo(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    accepted = 0
    lock = threading.Lock()

    def setup(self):
        with Echo.lock:
            Echo.accepted += 1
        super().setup()

    def do_GET(self):
        body = self.path.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class PoolTest(unittest.TestCase):
    def setUp(self):
        Echo.accepted = 0
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Echo)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.pool = httppool.Pool(f"http://127.0.0.1:{self.srv.server_address[1]}", 10)

    def tearDown(self):
        self.pool.close()
        self.srv.shutdown()
        self.srv.server_close()

    def test_threads_share_connections(self):
        def work(k):
            for i in range(50):
                status, _, body = self.pool.request("GET", f"/{k}/{i}")
                assert (status, body) == (200, f"/{k}/{i}".encode())
        ts = [threading.Thread(target=work, args=(k,)) for k in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertLessEqual(Echo.accepted, 8)  # 400 requests on at most one socket per thread
        self.assertLessEqual(len(self.pool._idle), httppool.IDLE_MAX)

    def test_a_dropped_idle_connection_is_retried_once(self):
        self.pool.request("GET", "/a")
        self.pool._idle[0].sock.close()  # as if the server had dropped it while idle
        self.assertEqual(self.pool.request("GET", "/b")[2], b"/b")

    def test_close_closes_idle_and_returned_connections(self):
        self.pool.request("GET", "/a")
        idle = list(self.pool._idle)
        self.pool.close()
        self.assertTrue(all(c.sock is None for c in idle))
        self.pool.request("GET", "/b")  # still works, but nothing is kept afterwards
        self.assertEqual(self.pool._idle, [])


if __name__ == "__main__":
    unittest.main()
