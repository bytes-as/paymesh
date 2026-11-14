"""paymesh system launcher — run the whole thing locally with one command.

    py -3 paymesh.py            run the system in the foreground (Ctrl-C stops)
    py -3 paymesh.py start      run detached in the background (logs: paymesh.log)
    py -3 paymesh.py stop       stop the detached system
    py -3 paymesh.py status     is it up? routes and ports

What runs: the mini blockchain (chain.json), the paid API on :8402 and the
explorer on :8403, all in one process. `start` detaches it (Windows: a detached process;
Linux/macOS: a new session, so closing the terminal doesn't stop it) and writes its pid to
paymesh.pid next to the chain (PAYMESH_HOME); `stop` asks it to exit (SIGTERM / taskkill),
waits, and forces it after 10 s. The live flows are then:
    py -3 buyer_agent.py        agent pays for /premium/price and /premium/history
    open http://127.0.0.1:8403  watch the chain, txs and balances

Stdlib only.
"""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
HOME = os.environ.get("PAYMESH_HOME") or HERE  # the data folder, as wallets.py and chain.py use it
PID_FILE = os.path.join(HOME, "paymesh.pid")
LOG_FILE = os.path.join(HOME, "paymesh.log")
API = f"http://127.0.0.1:{os.environ.get('PAYMESH_API_PORT', 8402)}"


def serve_forever():
    import threading
    import chain
    import durable
    import server
    import explorer
    stop_now = threading.Event()
    if hasattr(signal, "SIGTERM"):  # `paymesh.py stop` on Linux/macOS: exit cleanly, logs synced
        signal.signal(signal.SIGTERM, lambda *a: stop_now.set())
    c = chain.Chain.open_or_archive()
    server.start(c)
    explorer.start(c, prices=server.PRICES)
    try:
        while not stop_now.wait(1.0):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        durable.close_all()


def _pid():
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, someone else's
    try:  # a zombie child of this process counts as gone
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


def start():
    pid = _pid()
    if status(quiet=True) or (pid and _alive(pid)):
        print(f"already running (pid {pid})" if pid else "already running")
        return
    os.makedirs(HOME, exist_ok=True)
    log = open(LOG_FILE, "ab")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True  # its own session: the terminal closing doesn't stop it
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve"], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, cwd=HOME, **kw)
    log.close()
    with open(PID_FILE, "w") as f:
        f.write(str(p.pid))
    deadline = time.time() + 15
    while time.time() < deadline and not status(quiet=True):
        if p.poll() is not None:
            break
        time.sleep(0.3)
    if status(quiet=True):
        print("running")
    else:
        print(f"failed to start — see {LOG_FILE}")
        if p.poll() is not None:
            os.remove(PID_FILE)


def stop():
    pid = _pid()
    if pid is None:
        print("not running (no pid file)")
        return
    if not _alive(pid):
        os.remove(PID_FILE)
        print(f"not running (stale pid file for {pid} removed)")
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    else:
        os.kill(pid, signal.SIGTERM)  # serve_forever exits cleanly on it
    deadline = time.time() + 10
    while time.time() < deadline and _alive(pid):
        time.sleep(0.1)
    if _alive(pid) and os.name != "nt":
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
    os.remove(PID_FILE)
    while time.time() < deadline and status(quiet=True):  # the port is free once it answers no more
        time.sleep(0.1)
    print(f"stopped pid {pid}")


def status(quiet=False) -> bool:
    up = False
    try:
        with urllib.request.urlopen(f"{API}/health", timeout=2) as r:
            up = r.status == 200
            health = json.loads(r.read().decode())
    except Exception:
        health = {}
    if not quiet:
        print("up" if up else "down", json.dumps(health))
        print(f"api:      {API}  (prices: "
              + "  ".join(f"{p}={v} PMX" for p, v in sorted(PRICES_DISPLAY())) + ")")
        print(f"explorer: http://127.0.0.1:{os.environ.get('PAYMESH_EXPLORER_PORT', 8403)}")
    return up


def PRICES_DISPLAY():
    try:
        with urllib.request.urlopen(f"{API}/open/info", timeout=2) as r:
            return json.loads(r.read().decode())["endpoints"].items()
    except Exception:
        return {}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "start":
        start()
    elif cmd == "stop":
        stop()
    elif cmd == "status":
        status()
    elif cmd == "serve":
        serve_forever()
    else:
        serve_forever()
