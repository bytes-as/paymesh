"""paymesh buyer agent: discovers 402, pays, retries, logs each payment.

buyer_broke.py reuses this module with an empty wallet: it must be refused.
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

API = "http://127.0.0.1:8402"
WAIT_FOR_MINE = 1.0  # give the server a beat after we mine (server mines on its thread)


def get(path: str, headers: dict | None = None):
    req = urllib.request.Request(API + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
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
    if bal < amount:
        log["result"] = f"insufficient funds ({bal} < {amount} PMX)"
        log["ok"] = False
        return log
    tx = chain_.new_tx(wallet.address, challenge["recipient"], amount, memo)
    wallets.sign_tx(tx, wallet)
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
    c = chain.Chain()
    if not c.blocks:
        c.genesis()
    w = wallets.load_or_create("buyer")
    funded = ensure_funded(c, w, 20)
    if funded:
        print(f"faucet: funded '{w.name}' with 100 PMX")
    i = 0
    for path in ("/premium/price", "/premium/history"):
        i += 1
        log = buy_once(c, w, "buyer", i, path)
        print(json.dumps(log))
        if not log["ok"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
