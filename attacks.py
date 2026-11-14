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


def run(node: str, restart=None) -> list:
    """restart(): kill the node without cleanup and start it again on the same data (scratch only)."""
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
    # WOTS v2: a signature is bound to its key's public seed and to each chain position
    t5 = signed(me, seller, 1, "wots2")
    attempt("strip the public seed off a WOTS v2 signature to pass it as v1",
            lambda: post(dict(t5, sig=t5["sig"][1:])), lambda c, b: c == 400 and "not signed by" in str(b))
    attempt("move a WOTS v2 signature under another key's public seed",
            lambda: post(dict(t5, sig=[victim.wallet.key(0)[2].hex()] + t5["sig"][1:])),
            lambda c, b: c == 400 and "not signed by" in str(b))
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
    kept = []  # play a seller that refuses a buyer's signed tx but keeps a copy
    real_pay = victim._pay
    victim._pay = lambda path, acc, payload: (kept.append(payload.get("transaction")), real_pay(path, acc, payload))[1]
    o = fresh()
    try:  # an unknown challenge: the seller refuses, the victim retires the key that signed it
        victim._on_chain("/premium/price", dict(o["tx"], extra=dict(o["tx"]["extra"], memo=f"{seller}:{'0' * 32}:5")))
    except Exception:  # noqa: BLE001 - PaymentError expected
        pass
    victim._pay = real_pay
    attempt("x402: a seller mines a refused tx it kept", lambda: post(kept[0]), no)
    # --- channel expiry (QA-PM-1, QA-PM-2) ---
    attempt("channel tick written as a superscript 2 to crash the request",
            lambda: me._req("/premium/price", headers={"X-Channel": f"{me.channel.id} ² {'ab' * 32}"}),
            lambda c, b: c == 402)
    import channels as chn
    height = lambda: me._node("/health")[1]["chain_blocks"]  # noqa: E731

    def raw_channel(expires_in):
        bc = chn.BuyerChannel(50)
        led = victim._ledger()
        t = led.new_tx(victim.wallet.address, seller, 50, "channel:open")
        t.update(kind="channel_open", anchor=bc.anchor, ticks=50, unit=1, expires=height() + expires_in)
        wallets.sign_tx(t, victim.wallet, led)
        post(t)
        bc.id = t["txid"]
        return bc, t["expires"]

    near, _ = raw_channel(2)
    attempt("buy on a channel that is refundable next block, then refund it",
            lambda: me._req("/premium/price", headers={"X-Channel": near.pay(5)}), lambda c, b: c == 402)
    race, exp = raw_channel(chn.SellerBook.STOP_WITHIN + 3)
    served = [me._req("/premium/price", headers={"X-Channel": race.pay(5)})[0] for _ in range(2)].count(200)
    while height() < exp:
        me._node(f"/faucet?address={'ee' * 32}")  # each mint is a block: race the settler

    def refund():
        led = victim._ledger()
        t = led.new_tx(victim.wallet.address, victim.wallet.address, 50, "channel:refund")
        t.update(kind="channel_refund", channel=race.id)
        wallets.sign_tx(t, victim.wallet, led)
        post(t)
        return 200, me._node(f"/channel/info?id={race.id}")[1]
    attempt("spend a channel, mine blocks fast, refund before the seller settles", refund,
            lambda c, b: served > 0 and b.get("paid") == served * 5)
    attempt("wallet name that escapes the wallet folder", lambda: me._node("/faucet?name=../../evil"), no)
    attempt("malformed JSON body", lambda: raw_post(node, "/tx", b"{"), lambda c, b: c == 400)
    # the same race against a seller running apart from the node (examples/weather_api.py)
    sys.path.insert(0, os.path.join(HERE, "examples"))
    import threading
    from wsgiref.simple_server import WSGIRequestHandler, make_server
    import weather_api

    class Quiet(WSGIRequestHandler):
        def log_message(self, *a):
            pass
    shop, app = weather_api.build(node)
    sport = _free_port()
    httpd = make_server("127.0.0.1", sport, app, server_class=weather_api.ThreadingWSGIServer, handler_class=Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        bc = chn.BuyerChannel(30)
        led = victim._ledger()
        t = led.new_tx(victim.wallet.address, shop.wallet.address, 30, "channel:open")
        exp = height() + chn.SellerBook.STOP_WITHIN + 3
        t.update(kind="channel_open", anchor=bc.anchor, ticks=30, unit=1, expires=exp)
        wallets.sign_tx(t, victim.wallet, led)
        post(t)
        bc.id = t["txid"]
        shop_api = Client(f"http://127.0.0.1:{sport}", "attacker", node=node)
        served_far = [shop_api._req("/forecast", headers={"X-Channel": bc.pay(3)})[0] for _ in range(3)].count(200)
        while height() < exp:
            me._node(f"/faucet?address={'ee' * 32}")

        def refund_far():
            led = victim._ledger()
            t = led.new_tx(victim.wallet.address, victim.wallet.address, 30, "channel:refund")
            t.update(kind="channel_refund", channel=bc.id)
            wallets.sign_tx(t, victim.wallet, led)
            post(t)
            return 200, me._node(f"/channel/info?id={bc.id}")[1]
        attempt("the same refund race against a seller running apart from the node", refund_far,
                lambda c, b: served_far > 0 and b.get("paid") == served_far * 3)
    finally:
        shop.stop.set()
        httpd.shutdown()
        httpd.server_close()

    if restart:  # the seller logs a tick before it serves: a crash can't make a word new again
        h = me.channel.pay(5)
        first = me._req("/premium/price", headers={"X-Channel": h})[0]
        restart()
        attempt("replay a channel word after the node was killed and restarted",
                lambda: me._req("/premium/price", headers={"X-Channel": h}),
                lambda c, b: first == 200 and c == 402 and "not after the last paid tick" in str(b))
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
    procs, restart = [], None
    if node is None:
        home, port = tempfile.mkdtemp(), _free_port()
        env = dict(os.environ, PAYMESH_HOME=home, PAYMESH_API_PORT=str(port), PAYMESH_EXPLORER_PORT=str(_free_port()))
        node = f"http://127.0.0.1:{port}"

        def start():
            procs.append(subprocess.Popen([sys.executable, os.path.join(HERE, "paymesh.py"), "serve"], env=env,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            for _ in range(100):
                try:
                    urllib.request.urlopen(node + "/health", timeout=1).close()
                    return
                except OSError:
                    time.sleep(0.1)

        def restart():
            procs[-1].kill()  # TerminateProcess: no shutdown code runs
            procs[-1].wait()
            start()
        start()
    try:
        res = run(node, restart)
    finally:
        for p in procs:
            p.kill()
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
