"""paymesh buyer agent: discovers 402, pays, retries, logs each payment.

Stdlib only. Example:
    py -3 buyer_agent.py
"""
import json
import os
import sys
import urllib.request
import urllib.error
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chain
import wallets

API = os.environ.get("PAYMESH_API", f"http://127.0.0.1:{os.environ.get('PAYMESH_API_PORT', 8402)}")
WAIT_FOR_MINE = 1.0  # give the server a beat after we mine (server mines on its thread)


def get(path: str, headers: dict | None = None):
    req = urllib.request.Request(API + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        with e:
            body = e.read().decode()
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            pass
        return e.code, body


def buy_once(chain_: chain.Chain, wallet: wallets.Wallet, name: str, call: int,
             path: str = "/premium/price") -> dict:
    """One paid call cycle. Returns a log dict."""
    log = {"agent": name, "call": call, "endpoint": path,
           "wallet": wallet.address[:16] + "..."}

    # 1. ask -> expect 402 challenge
    status, body = get(path)
    if status != 402:
        log["result"] = f"expected 402, got {status}"
        log["ok"] = False
        return log
    challenge = body
    memo = challenge["memo"]
    amount = challenge["amount"]  # pay exactly what the route quotes
    log["challenge"] = f"{challenge['nonce'][:12]}... {amount} PMX"

    # 2. check wallet (fresh read: the running server mines and persists), pay, submit
    bal = chain.Chain(chain_.path).balance(wallet.address)
    if bal < amount + 1:  # + the tx fee (1 PMX for a WOTS tx)
        log["result"] = f"insufficient funds ({bal} < {amount} PMX + fee)"
        log["ok"] = False
        return log
    live = chain.Chain(chain_.path)  # the running server's ledger: whose key signs next
    tx = live.new_tx(wallet.address, challenge["recipient"], amount, memo)
    wallets.sign_tx(tx, wallet, live)
    req = urllib.request.Request(f"{API}/tx", data=json.dumps(tx).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        log["result"] = f"tx rejected: {json.loads(e.read().decode())['error']}"
        log["ok"] = False
        return log
    log["txid"] = res["txid"][:16] + "..."
    log["block"] = res["block"]
    log["amount"] = amount

    # 3. retry with X-Payment
    status, body = get(path, {"X-Payment": tx["txid"]})
    log["result"] = f"{status} {body.get('price', body.get('count', body.get('error')))}"
    log["ok"] = status == 200
    return log


def ensure_funded(chain_: chain.Chain, wallet: wallets.Wallet, need: int) -> bool:
    """Mint from the running API's free faucet if the wallet can't cover a call."""
    if chain.Chain(chain_.path).balance(wallet.address) >= need:
        return False
    with urllib.request.urlopen(f"{API}/faucet?name={wallet.name}", timeout=5) as r:
        json.loads(r.read().decode())
    time.sleep(WAIT_FOR_MINE)
    return True


def main():
    """The demo: two calls paid on-chain, then a payment channel for many fast calls."""
    import time
    from paymesh_client import Client
    api = Client(API, "buyer")
    if api.balance() < 150:
        for _ in range(2):
            api.faucet()
        print(f"faucet: funded 'buyer' to {api.balance()} PMX")
    for path in ("/premium/price", "/premium/history"):  # on-chain: 402 -> signed tx -> retry
        t0 = time.perf_counter()
        code, body = api.get(path)
        print(json.dumps({"mode": "on-chain", "endpoint": path, "status": code,
                          "result": body.get("price", body.get("count", body.get("error"))),
                          "ms": round((time.perf_counter() - t0) * 1000, 1)}))
        if code != 200:
            sys.exit(1)
    if api.channel is None:
        cid = api.open_channel(ticks=100)  # one deposit on-chain: 100 PMX
        print(json.dumps({"mode": "channel", "opened": cid[:16] + "...", "deposit": 100}))
    t0 = time.perf_counter()
    n = 0
    while n < 20:
        code, body = api.get("/premium/price")  # one request each, a hash-chain word as payment
        if code != 200:
            sys.exit(1)
        n += 1
    dt = time.perf_counter() - t0
    print(json.dumps({"mode": "channel", "calls": n, "ms_per_call": round(dt / n * 1000, 2), "last_price": body["price"]}))
    res = api.close_channel()
    print(json.dumps({"mode": "channel", "settled": True, "paid_to_seller": res["paid_to_seller"],
                      "refunded": 100 - res["paid_to_seller"], "block": res["block"], "balance": api.balance()}))


if __name__ == "__main__":
    main()
