"""paymesh.py start / status / stop on this OS: start detaches (POSIX: its own session) and
writes the pid next to the chain (PAYMESH_HOME); a second start is refused; stop ends the
process and frees the port; a stale pid file is noticed and removed."""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import paymesh  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Launcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, PAYMESH_HOME=self.tmp.name, PAYMESH_API_PORT=str(free_port()),
                        PAYMESH_EXPLORER_PORT=str(free_port()))

    def tearDown(self):
        self.cli("stop")  # never leave a node behind, whatever failed
        self.tmp.cleanup()

    def cli(self, *args):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "paymesh.py"), *args], env=self.env,
                           capture_output=True, text=True, timeout=60)
        return r.stdout.strip()

    def test_start_status_stop(self):
        self.assertEqual(self.cli("start"), "running")
        pid_file = os.path.join(self.tmp.name, "paymesh.pid")
        pid = int(open(pid_file).read())
        self.assertTrue(paymesh._alive(pid))
        if hasattr(os, "getsid"):
            self.assertEqual(os.getsid(pid), pid, "a session of its own: the terminal closing must not stop it")
        self.assertTrue(self.cli("status").startswith("up"))
        self.assertIn("already running", self.cli("start"))
        self.assertEqual(self.cli("stop"), f"stopped pid {pid}")
        self.assertFalse(paymesh._alive(pid))
        self.assertFalse(os.path.exists(pid_file))
        self.assertTrue(self.cli("status").startswith("down"))
        self.assertEqual(self.cli("stop"), "not running (no pid file)")
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "chain.json")))  # its data stayed in PAYMESH_HOME

    def test_a_stale_pid_file(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        with open(os.path.join(self.tmp.name, "paymesh.pid"), "w") as f:
            f.write(str(p.pid))
        self.assertIn("stale pid file", self.cli("stop"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "paymesh.pid")))
        self.assertEqual(self.cli("start"), "running")  # a stale pid file doesn't block a start


if __name__ == "__main__":
    unittest.main()
