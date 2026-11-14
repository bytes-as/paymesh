"""paymesh system launcher — run the whole thing locally with one command.

    py -3 paymesh.py            run the system in the foreground (Ctrl-C stops)
    py -3 paymesh.py start      run detached in the background (logs: paymesh.log)
    py -3 paymesh.py stop       stop the detached system
    py -3 paymesh.py status     is it up? routes and ports

What runs: the mini blockchain (chain.json), the paid API on :8402 and the
explorer on :8403, all in one process. The live flows are then:
    py -3 buyer_agent.py        agent pays for /premium/price and /premium/history
    open http://127.0.0.1:8403  watch the chain, txs and balances

Stdlib only.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
PID_FILE = os.path.join(HERE, "paymesh.pid")
LOG_FILE = os.path.join(HERE, "paymesh.log")


def serve_forever():
    import chain
    import server
    import explorer
    c = chain.Chain()
    if not c.blocks:
        c.genesis()
    server.start(c)
    explorer.start(c, prices=server.PRICES)
    try:
        while True:
            threading_wait = 3600
            import threading
            threading.Event().wait(threading_wait)
    except KeyboardInterrupt:
        pass


def start():
    if status(quiet=True):
        print("already running")
        return
    log = open(LOG_FILE, "ab")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve"],
                         stdout=log, stderr=log, cwd=HERE, creationflags=flags)
    with open(PID_FILE, "w") as f:
        f.write(str(p.pid))
    deadline = time.time() + 10
    while time.time() < deadline and not status(quiet=True):
        time.sleep(0.3)
    print("running" if status(quiet=True) else "failed to start — see paymesh.log")


def stop():
    if not os.path.exists(PID_FILE):
        print("not running (no pid file)")
        return
    pid = open(PID_FILE).read().strip()
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
    else:
        subprocess.run(["kill", pid], capture_output=True)
    os.remove(PID_FILE)
    print(f"stopped pid {pid}")


def status(quiet=False) -> bool:
    up = False
    try:
        with urllib.request.urlopen("http://127.0.0.1:8402/health", timeout=2) as r:
            up = r.status == 200
            health = json.loads(r.read().decode())
    except Exception:
        health = {}
    if not quiet:
        print("up" if up else "down", json.dumps(health))
        print("api:      http://127.0.0.1:8402  (prices: "
              + "  ".join(f"{p}={v} PMX" for p, v in sorted(PRICES_DISPLAY())) + ")")
        print("explorer: http://127.0.0.1:8403")
    return up


def PRICES_DISPLAY():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8402/open/info", timeout=2) as r:
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
