"""Try to cheat a paymesh node and show that every attempt is refused.

    py -3 attacks.py                 # starts its own scratch node (never the live one)
    py -3 attacks.py http://host:port   # or attack a node you run (it mints test PMX there)

Stdlib only.
"""
import http.client
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(node: str) -> list:
    import chain as ch
    import wallets
    from paymesh_client import Client

    wallets.WALLET_DIR = os.path.join(tempfile.mkdtemp(), "wallets")
    me = Client(node, "attacker")
    victim = Client(node, "victim")
    me.faucet()
    victim.faucet()
    seller = me._node("/channel/terms")[1]["seller"]
    results = []

    def attempt(name, fn, refused):
        try:
            code, body = fn()
        except Exception as e:  # noqa: BLE001
            code, body = "error", repr(e)
        ok = refused(code, body)
        results.append({"attack": name, "refused": ok, "answer": code, "detail": str(body)[:80]})

    def post(tx):
        return me._node("/tx", tx)

    def signed(frm, to, amount, memo="x", **extra):
        led = frm._ledger()
        tx = led.new_tx(frm.wallet.address, to, amount, memo)
        tx.update(extra)
        wallets.sign_tx(tx, frm.wallet, led)
        return tx

    no = lambda c, b: c != 200  # noqa: E731
    attempt("spend more than the balance", lambda: post(signed(me, seller, 10_000)), no)
    attempt("negative amount to pull money from a victim", lambda: post(signed(me, victim.wallet.address, -50)), no)
    attempt("mint 10^12 PMX with a hand-made faucet tx",
            lambda: post(ch.Chain.new_tx(ch.FAUCET, me.wallet.address, 10**12, "faucet:me")), no)
    tx = signed(me, seller, 1, "honest")
    post(tx)
    attempt("resubmit someone's signed tx to charge them twice", lambda: post(tx), no)
    t2 = signed(me, seller, 1, "stapled")
    attempt("staple junk fields onto a signed tx", lambda: post(dict(t2, junk="x" * 50_000)), no)
    t3 = dict(signed(victim, seller, 1, "redirect"), to_wallet=me.wallet.address)
    attempt("redirect a victim's signed payment to myself", lambda: post(t3), no)
    t4 = ch.Chain.new_tx(victim.wallet.address, me.wallet.address, 50, "forged")
    t4.update(next_key="e" * 64, sig=["ab" * 32] * 67)
    attempt("forge a victim's signature", lambda: post(t4), no)
    attempt("call a paid route without paying", lambda: me._req("/premium/price"), lambda c, b: c == 402)
    attempt("pay with a made-up txid", lambda: me._req("/premium/price", headers={"X-Payment": "ab" * 32}),
            lambda c, b: c == 402)
    attempt("pay a channel call with a word off the hash chain",
            lambda: (me.open_channel(ticks=10), me._req("/premium/price", headers={"X-Channel": f"{me.channel.id} 5 {'cd' * 32}"}))[1],
            lambda c, b: c == 402)
    h = me.channel.pay(5)
    me._req("/premium/price", headers={"X-Channel": h})
    attempt("reuse a channel word that was already spent", lambda: me._req("/premium/price", headers={"X-Channel": h}),
            lambda c, b: c == 402)
    attempt("underpay through the channel (1 tick for a 5 PMX route)",
            lambda: me._req("/premium/price", headers={"X-Channel": f"{me.channel.id} 6 {me.channel.words[6]}"}),
            lambda c, b: c == 402)
    # --- the same, in x402 v2 headers (PAYMENT-SIGNATURE) ---
    import paymesh_x402 as x402
    def x402_pay(accepted, payload, version=2):
        return me._req("/premium/price", headers={x402.SIGNATURE: x402.encode(
            {"x402Version": version, "accepted": accepted, "payload": payload})})

    def fresh():
        me._req("/premium/price")  # a 402 with a new challenge in PAYMENT-REQUIRED
        return me._offers("/premium/price")

    o = fresh()
    paid = signed(me, seller, 5, o["tx"]["extra"]["memo"])
    x402_pay(o["tx"], {"transaction": paid})
    attempt("x402: replay a PAYMENT-SIGNATURE that already paid", lambda: x402_pay(o["tx"], {"transaction": paid}),
            lambda c, b: c == 402)
    o = fresh()
    attempt("x402: claim a 1 PMX quote for a 5 PMX route",
            lambda: x402_pay(dict(o["tx"], amount="1"), {"transaction": signed(me, seller, 1, o["tx"]["extra"]["memo"])}),
            lambda c, b: c == 400)
    attempt("x402: settle a tx that pays myself instead of the seller",
            lambda: x402_pay(o["tx"], {"transaction": signed(me, me.wallet.address, 5, o["tx"]["extra"]["memo"])}),
            lambda c, b: c == 402)
    attempt("x402: pay a channel call with a word off the hash chain",
            lambda: x402_pay(o["channel"], {"channel": me.channel.id, "tick": 7, "word": "cd" * 32}),
            lambda c, b: c == 402)
    attempt("x402: pay with another chain's network id",
            lambda: x402_pay(dict(o["tx"], network="paymesh:" + "0" * 32), {"txid": paid["txid"]}),
            lambda c, b: c == 400)
    attempt("x402: garbage PAYMENT-SIGNATURE",
            lambda: me._req("/premium/price", headers={x402.SIGNATURE: "e30=" * 9000}), lambda c, b: c == 400)
    attempt("wallet name that escapes the wallet folder", lambda: me._node("/faucet?name=../../evil"), no)
    attempt("malformed JSON body", lambda: raw_post(node, "/tx", b"{"), lambda c, b: c == 400)
    attempt("node still healthy afterwards", lambda: me._node("/health"), lambda c, b: c == 200)
    return results


def raw_post(node: str, path: str, body: bytes):
    u = urllib.parse.urlsplit(node)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    try:
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        return r.status, r.read().decode()[:80]
    finally:
        conn.close()


def main():
    node = sys.argv[1] if len(sys.argv) > 1 else None
    proc = None
    if node is None:
        home, port = tempfile.mkdtemp(), _free_port()
        env = dict(os.environ, PAYMESH_HOME=home, PAYMESH_API_PORT=str(port), PAYMESH_EXPLORER_PORT=str(_free_port()))
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "paymesh.py"), "serve"], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        node = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(node + "/health", timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
    try:
        res = run(node)
    finally:
        if proc:
            proc.kill()
    width = max(len(r["attack"]) for r in res)
    for r in res:
        mark = "REFUSED" if r["refused"] else "ACCEPTED (!)"
        if r["attack"].startswith("node still"):
            mark = "OK" if r["refused"] else "DOWN (!)"
        print(f"{r['attack']:<{width}}  {mark:<12} {r['answer']}")
    bad = [r for r in res if not r["refused"]]
    print(f"\n{len(res) - len(bad)}/{len(res)} as expected")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
