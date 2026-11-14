"""Hub payouts, watched live: two sellers earn from one buyer's hub channel, and their on-chain
balances move every few seconds, long before the channel closes.

    py -3 examples/payout_demo.py [seconds] [payout interval s]

Starts its own scratch node (a temp folder, free ports); never touches the live chain.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 15
    every = sys.argv[2] if len(sys.argv) > 2 else "3"
    tmp = tempfile.mkdtemp(prefix="paymesh-payout-demo-")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ, PAYMESH_HOME=os.path.join(tmp, "node"), PAYMESH_API_PORT=str(port),
               PAYMESH_EXPLORER_PORT=str(free_port()), PAYMESH_HUB_PAYOUT_S=every, PAYMESH_HUB_PAYOUT_MIN="1000000")
    os.makedirs(env["PAYMESH_HOME"])
    node = subprocess.Popen([sys.executable, os.path.join(ROOT, "paymesh.py"), "serve"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(200):
            try:
                urllib.request.urlopen(url + "/health", timeout=1).close()
                break
            except OSError:
                time.sleep(0.05)
        import wallets
        wallets.WALLET_DIR = os.path.join(tmp, "buyer")
        from paymesh_client import Client
        buyer = Client(url, "demo-buyer")
        buyer.faucet()
        buyer.faucet()
        buyer.open_hub(ticks=150)
        cid = buyer.channel.id

        def get(path):
            with urllib.request.urlopen(url + path, timeout=10) as r:
                return json.load(r)

        def claim(seller):
            c, i, w = buyer.channel.pay(1).split()
            body = json.dumps({"channel": c, "tick": int(i), "word": w, "seller": seller, "price": 1}).encode()
            req = urllib.request.Request(url + "/hub/claim", body, {"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()

        sellers = {"weather": "a1" * 32, "prices": "b2" * 32}
        earned = {k: 0 for k in sellers}
        print(f"hub payout every {every} s; each seller earns 1 PMX per call\n")
        print(f"{'t':>5}  {'weather earned':>14} {'on chain':>8}   {'prices earned':>13} {'on chain':>8}   paid ticks  open")
        t0 = time.time()
        next_print = 0.0
        n = 0
        while time.time() - t0 < seconds:
            name = "weather" if n % 3 else "prices"  # weather sells twice as many calls
            claim(sellers[name])
            earned[name] += 1
            n += 1
            time.sleep(0.15)
            if time.time() - t0 >= next_print:
                bal = {k: get(f"/account?address={a}")["balance"] for k, a in sellers.items()}
                ch = get(f"/channel/info?id={cid}")
                print(f"{time.time() - t0:5.1f}  {earned['weather']:>14} {bal['weather']:>8}   {earned['prices']:>13} "
                      f"{bal['prices']:>8}   {ch.get('paid_ticks', 0):>10}  {ch['open']}")
                next_print += 1.0
        time.sleep(float(every) + 1)
        bal = {k: get(f"/account?address={a}")["balance"] for k, a in sellers.items()}
        print(f"\nafter the last payout: weather {bal['weather']} of {earned['weather']}, prices {bal['prices']} of "
              f"{earned['prices']} (each payout's 1 PMX fee comes out of the larger share); channel still open: "
              f"{get(f'/channel/info?id={cid}')['open']}")
        buyer.close()
    finally:
        node.kill()
        node.wait()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
