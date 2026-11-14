"""Benchmark: paid calls per second, on-chain vs payment channel vs hub channel.

Starts a separate paymesh server (its own PAYMESH_HOME and ports, never the
live one), then a client with its own wallet folder pays for /premium/price:
    py -3 bench.py [on_chain_calls] [channel_calls]
"""
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(n_chain: int, n_chan: int):
    home, client_home = tempfile.mkdtemp(prefix="paymesh-bench-"), tempfile.mkdtemp(prefix="paymesh-bench-")
    import atexit, shutil  # noqa: E401
    for d in (home, client_home):
        atexit.register(shutil.rmtree, d, True)  # scratch chain and wallets go when the run ends
    port, eport = free_port(), free_port()
    env = dict(os.environ, PAYMESH_HOME=home, PAYMESH_API_PORT=str(port), PAYMESH_EXPLORER_PORT=str(eport),
               PAYMESH_FAUCET_BURST="100000", PAYMESH_TX_BURST="100000", PAYMESH_CHALLENGE_BURST="100000")
    srv = subprocess.Popen([sys.executable, os.path.join(HERE, "paymesh.py"), "serve"], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "/health", timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        import wallets
        wallets.WALLET_DIR = os.path.join(client_home, "wallets")  # the client keeps its own keys
        from paymesh_client import Client

        def chain_bytes():
            return os.path.getsize(os.path.join(home, "chain.json"))

        c = Client(url, "bench")
        for _ in range((n_chain + 2 * n_chan) * 5 // 100 + 3):  # 5 PMX per call, 100 per faucet
            c.faucet()
        out = {}

        # on-chain: every call is 402 -> signed tx -> mined block -> retry
        b0, lat = chain_bytes(), []
        t0 = time.perf_counter()
        for _ in range(n_chain):
            t = time.perf_counter()
            code, _ = c.get("/premium/price")
            assert code == 200
            lat.append(time.perf_counter() - t)
        dt = time.perf_counter() - t0
        out["on_chain"] = {"calls": n_chain, "calls_per_s": round(n_chain / dt, 1),
                           "p50_ms": round(statistics.median(lat) * 1000, 2),
                           "p95_ms": round(sorted(lat)[int(len(lat) * 0.95) - 1] * 1000, 2),
                           "chain_bytes_per_call": (chain_bytes() - b0) // n_chain}

        # channel: one open, n calls carrying a hash-chain word, one close
        b0 = chain_bytes()
        t0 = time.perf_counter()
        c.open_channel(ticks=n_chan * 5)
        t_open = time.perf_counter() - t0
        c.get("/premium/price")  # learn the price
        lat = []
        t1 = time.perf_counter()
        for _ in range(n_chan - 1):
            t = time.perf_counter()
            code, _ = c.get("/premium/price")
            assert code == 200
            lat.append(time.perf_counter() - t)
        dt_calls = time.perf_counter() - t1
        t2 = time.perf_counter()
        settled = c.close_channel()
        t_close = time.perf_counter() - t2
        out["channel"] = {"calls": n_chan, "calls_per_s": round((n_chan - 1) / dt_calls, 1),
                          "p50_ms": round(statistics.median(lat) * 1000, 2),
                          "p95_ms": round(sorted(lat)[int(len(lat) * 0.95) - 1] * 1000, 2),
                          "open_ms": round(t_open * 1000, 1), "close_ms": round(t_close * 1000, 1),
                          "paid_to_seller": settled["paid_to_seller"],
                          "chain_bytes_per_call": (chain_bytes() - b0) // n_chan}
        out["speedup"] = round(out["channel"]["calls_per_s"] / out["on_chain"]["calls_per_s"], 1)

        # hub channel: one deposit with the node's hub (which pays any seller), n calls at this
        # API, one close. Each word goes through the hub's book (one counter per channel); the
        # node's own payout schedule (default: every 60 s or at 50 PMX owed) runs meanwhile.
        b0 = chain_bytes()
        c.open_hub(ticks=n_chan * 5)
        c.get("/premium/price")
        lat = []
        t1 = time.perf_counter()
        for _ in range(n_chan - 1):
            t = time.perf_counter()
            code, _ = c.get("/premium/price")
            assert code == 200
            lat.append(time.perf_counter() - t)
        dt_calls = time.perf_counter() - t1
        cid = c.channel.id
        c.close_channel()
        with urllib.request.urlopen(f"{url}/channel/info?id={cid}", timeout=10) as r:
            info = json.load(r)
        out["hub_channel"] = {"calls": n_chan, "calls_per_s": round((n_chan - 1) / dt_calls, 1),
                              "p50_ms": round(statistics.median(lat) * 1000, 2),
                              "p95_ms": round(sorted(lat)[int(len(lat) * 0.95) - 1] * 1000, 2),
                              "paid_out": info.get("paid"), "chain_bytes_per_call": (chain_bytes() - b0) // n_chan}

        # many agents at once, each with its own wallet and channel
        import threading
        agents, per = 8, max(50, n_chan // 4)
        clients = []
        for k in range(agents):
            a = Client(url, f"agent{k}")
            for _ in range(per * 5 // 100 + 1):
                a.faucet()
            a.open_channel(ticks=per * 5)
            a.get("/premium/price")
            clients.append(a)
        errors = []

        def work(a):
            try:
                for _ in range(per - 1):
                    assert a.get("/premium/price")[0] == 200
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
        threads = [threading.Thread(target=work, args=(a,)) for a in clients]
        t0 = time.perf_counter()
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        dt = time.perf_counter() - t0
        settled = sum(a.close_channel()["paid_to_seller"] for a in clients)
        out["parallel_channels"] = {"agents": agents, "calls": agents * (per - 1), "calls_per_s": round(agents * (per - 1) / dt, 1),
                                    "errors": len(errors), "paid_to_seller": settled}
        return out
    finally:
        srv.kill()
        srv.wait()


if __name__ == "__main__":
    a = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    b = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
    print(json.dumps(run(a, b), indent=1))
