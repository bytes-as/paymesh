"""The README quickstart, as separate processes on non-default ports: a node (paymesh.py serve),
the weather seller (examples/weather_api.py) and examples/quickstart.py, which must pay the node
on-chain and by channel and both sellers from one hub deposit. Every process gets the same port
variables, as the README says to do when 8402-8404 are taken."""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1).close()
            return True
        except urllib.error.HTTPError as e:
            e.close()
            return True  # a 402 is an answer
        except OSError:
            time.sleep(0.1)
    return False


class Quickstart(unittest.TestCase):
    def test_the_readme_quickstart_runs_on_other_ports(self):
        with tempfile.TemporaryDirectory() as home:
            api, expl, shop = free_port(), free_port(), free_port()
            env = dict(os.environ, PAYMESH_HOME=home, PAYMESH_API_PORT=str(api),
                       PAYMESH_EXPLORER_PORT=str(expl), WEATHER_PORT=str(shop))
            env.pop("PAYMESH_API", None)
            procs = []
            try:
                for script in ("paymesh.py serve", "examples/weather_api.py"):
                    argv = [sys.executable] + [os.path.join(ROOT, script.split()[0])] + script.split()[1:]
                    procs.append(subprocess.Popen(argv, env=env, cwd=home, stdout=subprocess.DEVNULL,
                                                  stderr=subprocess.DEVNULL))
                    self.assertTrue(wait_http(f"http://127.0.0.1:{api}/health" if "serve" in script
                                              else f"http://127.0.0.1:{shop}/forecast"), script)
                r = subprocess.run([sys.executable, os.path.join(ROOT, "examples", "quickstart.py")], env=env, cwd=home,
                                   capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                for line in ("1. on-chain:  200", "2. channel:   200", "3. hub:       200", "(weather seller, same deposit)"):
                    self.assertIn(line, r.stdout)
            finally:
                for p in procs:
                    p.kill()
                    p.wait()


if __name__ == "__main__":
    unittest.main()
