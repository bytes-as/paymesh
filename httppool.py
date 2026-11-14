"""A small pool of kept-alive HTTP connections to one server (stdlib only).

Used by the client SDK and the seller middleware to reach a node or another seller. A
connection is borrowed for one request and handed back, so threads share a few sockets
instead of each opening its own: a threaded server makes a thread per connection, and a
per-thread socket was left open (until garbage collection) when that thread ended.
close() closes the idle ones; one borrowed at that moment is closed when it comes back.
"""
import http.client
import socket
import threading
import urllib.parse

IDLE_MAX = 16  # idle connections kept per server; more are closed when handed back


class Pool:
    def __init__(self, base: str, timeout: float):
        u = urllib.parse.urlsplit(base)
        self.host, self.port, self.prefix = u.hostname, u.port or 80, u.path.rstrip("/")
        self.timeout = timeout
        self._idle: list = []
        self._lock = threading.Lock()
        self.closed = False

    def _new(self) -> http.client.HTTPConnection:
        """Connect blocking, then set the timeout (a timed connect costs ~15 ms on Windows),
        and TCP_NODELAY (no Nagle wait on small requests)."""
        c = http.client.HTTPConnection(self.host, self.port)
        c.sock = socket.create_connection((self.host, self.port))
        c.sock.settimeout(self.timeout)
        c.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return c

    def _get(self):
        with self._lock:
            if self._idle:
                return self._idle.pop(), False
        return self._new(), True

    def _put(self, c):
        with self._lock:
            if not self.closed and len(self._idle) < IDLE_MAX:
                self._idle.append(c)
                return
        c.close()

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        """(status, headers, body bytes). An idle connection the server closed meanwhile is
        retried once on a new one: that request never reached the server. A failure on a new
        connection is raised (the server is down)."""
        for attempt in range(2):
            c, fresh = self._get()
            try:
                c.request(method, self.prefix + path, body=body, headers=headers or {})
                r = c.getresponse()
                raw = r.read()
            except (http.client.HTTPException, OSError):
                c.close()
                if fresh or attempt:
                    raise
                continue
            if r.will_close:
                c.close()
            else:
                self._put(c)
            return r.status, r.headers, raw
        raise OSError("unreachable")  # pragma: no cover

    def close(self):
        with self._lock:
            self.closed = True
            idle, self._idle = self._idle, []
        for c in idle:
            c.close()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter shutdown
            pass
