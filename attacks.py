"""Try to cheat a paymesh node and show that every attempt is refused.

    py -3 attacks.py                 # starts its own scratch node (never the live one)
    py -3 attacks.py http://host:port   # or attack a node you run (it mints test PMX there)

Stdlib only.
"""
import atexit
import http.client
import json
import os
import shutil
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

    scratch = tempfile.mkdtemp(prefix="paymesh-attacks-")
    atexit.register(shutil.rmtree, scratch, True)  # scratch wallets go when the run ends
    wallets.WALLET_DIR = os.path.join(scratch, "wallets")
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
    # fees: 1 PMX per started 8 KB, signed over, so the chain can't be grown for free
    attempt("send a signed tx with a fee of 0", lambda: post(signed(me, seller, 1, "free ride", fee=0)),
            lambda c, b: c == 400 and "fee too low" in str(b))
    t6 = signed(me, seller, 1, "fee cut", fee=2)
    attempt("lower the fee of a signed tx (2 to 1, still above the minimum)", lambda: post(dict(t6, fee=1)),
            lambda c, b: c == 400 and "not signed by" in str(b))
    import json as _json
    import secrets as _secrets
    lam_w = wallets.Wallet("lamport-spam", _secrets.token_bytes(32), 0, None, None)  # an old 52 KB-signature key
    os.makedirs(wallets.WALLET_DIR, exist_ok=True)
    with open(lam_w.path, "w", encoding="utf-8") as f:
        _json.dump({"name": lam_w.name, "v": 3, "address": lam_w.address, "next_index": 0, "seed": lam_w.seed.hex()}, f)
    lam = Client(node, "lamport-spam")
    lam.faucet()
    attempt("put a 52 KB tx on chain for a 1 PMX fee", lambda: post(signed(lam, seller, 1, "big", fee=1)),
            lambda c, b: c == 400 and "fee too low" in str(b))
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
    o = fresh()  # QA-PM-4: two txs for one challenge, sent at once: only one may go on chain
    import threading
    led = me._ledger()
    two, txids = [], []
    for _ in range(2):
        t = led.new_tx(me.wallet.address, seller, 5, o["tx"]["extra"]["memo"])
        wallets.sign_tx(t, me.wallet, led)
        led._key = t["next_key"]  # the second signed by the next one-time key: both valid in order
        txids.append(t["txid"])
        two.append(x402.encode({"x402Version": 2, "accepted": o["tx"], "payload": {"transaction": t}}))

    def both_at_once():
        gate, codes = threading.Barrier(2), []

        def go(h):
            gate.wait()
            codes.append(raw_get(node, "/premium/price", {x402.SIGNATURE: h}))
        ts = [threading.Thread(target=go, args=(h,)) for h in two]
        [t.start() for t in ts]
        [t.join() for t in ts]
        return sorted(codes), sorted(me._node(f"/tx?id={i}")[0] for i in txids)  # 200 = mined, 404 = not
    attempt("x402: pay one challenge with two txs at once", both_at_once,
            lambda c, b: c.count(200) <= 1 and b.count(200) == c.count(200))  # mined == served, at most one
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
        victim.faucet()  # 50 locked + a 1 PMX fee per channel
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

    def near_x402():
        cid, i, w = near.pay(5).split()
        return x402_pay(fresh()["channel"], {"channel": cid, "tick": int(i), "word": w})
    attempt("x402: buy on a channel that is refundable next block", near_x402,
            lambda c, b: c == 402 and "too close to settle" in str(me.last_receipt()))
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
    # --- the x402 facilitator (/verify, /settle) that other resource servers delegate to ---
    net = me._node("/health")[1]["network"]
    shop_addr = "5a" * 32

    def fac(path, req, payload):
        return me._node(path, {"x402Version": 2, "paymentRequirements": req,
                               "paymentPayload": {"x402Version": 2, "accepted": req, "payload": payload}})
    me.faucet()
    req7 = x402.requirement(net, shop_addr, 7, "tx", 60, memo="shop:order-1:7")
    paid7 = signed(me, shop_addr, 7, "shop:order-1:7")
    fac("/settle", req7, {"transaction": paid7})
    attempt("facilitator: settle one payment twice (serve twice)", lambda: fac("/settle", req7, {"txid": paid7["txid"]}),
            lambda c, b: c == 200 and b.get("success") is False)
    req8 = x402.requirement(net, shop_addr, 7, "tx", 60, memo="shop:order-2:7")
    attempt("facilitator: pass a payment made for another order",
            lambda: fac("/settle", req8, {"transaction": signed(me, shop_addr, 7, "shop:order-1:7")}),
            lambda c, b: c == 200 and b.get("success") is False and b.get("errorReason") == "invalid_paymesh_challenge")
    ch_ = me.channel  # the next word, peeked: the facilitator must refuse it, so nothing is spent
    cid, i, w = (ch_.id, ch_.spent + 1, ch_.words[ch_.spent + 1]) if ch_ else (None, None, None)
    if cid:
        attempt("facilitator: settle a direct channel's word for another seller",
                lambda: fac("/settle", x402.requirement(net, shop_addr, 1, "channel", 60),
                            {"channel": cid, "tick": int(i), "word": w}),
                lambda c, b: c == 200 and b.get("success") is False)
    forged = ch.Chain.new_tx(victim.wallet.address, shop_addr, 7, "forged")
    forged.update(next_key="e" * 64, sig=["ab" * 32] * 68, fee=1)
    attempt("facilitator: settle a forged tx from a victim", lambda: fac("/settle", x402.requirement(net, shop_addr, 7, "tx", 60),
                                                                        {"transaction": forged}),
            lambda c, b: c == 200 and b.get("success") is False)
    # --- browsers: paid routes are CORS-open; nothing else answers a page on another site ---
    nu = urllib.parse.urlsplit(node)

    def from_evil_page(method, path, body=None, **extra):
        c = http.client.HTTPConnection(nu.hostname, nu.port, timeout=30)
        try:
            hdrs = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site", "Content-Type": "text/plain"}
            hdrs.update(extra)
            c.request(method, path, body=body, headers=hdrs)
            r = c.getresponse()
            data = r.read()
            return r.status, {"acao": r.getheader("Access-Control-Allow-Origin"), "body": data[:80].decode(errors="replace")}
        finally:
            c.close()
    v0 = victim.balance()
    attempt("browser: a foreign page mints coins with an <img> GET /faucet",
            lambda: from_evil_page("GET", f"/faucet?address={victim.wallet.address}"),
            lambda c, b: c == 403 and b["acao"] is None and victim.balance() == v0)
    evil_bc = chn.BuyerChannel(10)
    led = me._ledger()
    evil_open = led.new_tx(me.wallet.address, seller, 10, "channel:open")
    evil_open.update(kind="channel_open", anchor=evil_bc.anchor, ticks=10, unit=1, expires=height() + 50)
    wallets.sign_tx(evil_open, me.wallet, led)
    attempt("browser: a foreign page opens a channel (text/plain POST /tx)",
            lambda: from_evil_page("POST", "/tx", json.dumps(evil_open)),
            lambda c, b: c == 403 and b["acao"] is None and me._node(f"/tx?id={evil_open['txid']}")[0] == 404)
    attempt("browser: a foreign page reads a victim's /account",
            lambda: from_evil_page("GET", f"/account?address={victim.wallet.address}"),
            lambda c, b: c == 403 and b["acao"] is None)
    attempt("browser: preflight a JSON POST /tx from a foreign page",
            lambda: from_evil_page("OPTIONS", "/tx", **{"Access-Control-Request-Method": "POST",
                                                       "Access-Control-Request-Headers": "content-type"}),
            lambda c, b: c == 403 and b["acao"] is None)
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

        # --- the hub: one deposit, any seller ---
        hb = Client(node, "hubber")
        hb.faucet()
        hb.open_hub(ticks=40, expires_in=chn.SellerBook.STOP_WITHIN + 8)
        shop_url = f"http://127.0.0.1:{sport}/forecast"
        paid_calls = [hb.get("/premium/price")[0], hb.get(shop_url)[0]].count(200)  # 5 + 3 PMX
        word = hb.channel.pay(5)
        paid_calls += hb._req("/premium/price", headers={"X-Channel": word})[0] == 200
        attempt("spend one hub channel word at two sellers",
                lambda: hb._req(shop_url, headers={"X-Channel": word}), lambda c, b: c == 402)
        hub_exp = me._node(f"/channel/info?id={hb.channel.id}")[1]["expires"]
        while height() < hub_exp:
            me._node(f"/faucet?address={'ee' * 32}")

        def hub_refund():
            led = hb._ledger()
            t = led.new_tx(hb.wallet.address, hb.wallet.address, 40, "channel:refund")
            t.update(kind="channel_refund", channel=hb.channel.id)
            wallets.sign_tx(t, hb.wallet, led)
            post(t)
            return 200, me._node(f"/channel/info?id={hb.channel.id}")[1]
        attempt("spend a hub channel at two sellers, then race to refund it", hub_refund,
                lambda c, b: paid_calls == 3 and b.get("paid") == 13
                and sorted(b.get("splits", {}).values()) == [3, 9])  # 10 - the close's fee

        # the refund window: a seller that runs no settler at all (victim) keeps its money
        off = chn.BuyerChannel(10)
        me.faucet()
        led = me._ledger()
        t = led.new_tx(me.wallet.address, victim.wallet.address, 10, "channel:open")
        t.update(kind="channel_open", anchor=off.anchor, ticks=10, unit=1, expires=height() + 2)
        wallets.sign_tx(t, me.wallet, led)
        post(t)
        off.id = t["txid"]
        while height() < t["expires"]:
            me._node(f"/faucet?address={'ee' * 32}")

        def refund_tx_for(c):
            led = c._ledger()
            r = led.new_tx(c.wallet.address, c.wallet.address, 10, "channel:refund")
            r.update(kind="channel_refund", channel=off.id)
            wallets.sign_tx(r, c.wallet, led)
            return r
        post(refund_tx_for(me))  # step 1: opens the window
        attempt("claim a refund at once, while the seller is offline",
                lambda: post(refund_tx_for(me)), lambda c, b: c == 400 and "refund window open" in str(b))

        # a seller (me) inflating its close with splits beyond what the buyer revealed
        bc2 = chn.BuyerChannel(20)
        led = victim._ledger()
        t = led.new_tx(victim.wallet.address, me.wallet.address, 20, "channel:open")
        t.update(kind="channel_open", anchor=bc2.anchor, ticks=20, unit=1, expires=height() + 500)
        wallets.sign_tx(t, victim.wallet, led)
        post(t)
        bc2.id = t["txid"]
        attempt("a seller pays itself more than the buyer revealed, through splits",
                lambda: post(signed(me, victim.wallet.address, 2, "channel:close", kind="channel_close",
                                    channel=bc2.id, ticks=2, word=bc2.words[2], splits={me.wallet.address: 20})), no)
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
    if restart:  # QA-PM-4 follow-up: force "not mined in time" (the node waits 0 s for the block)
        restart(PAYMESH_MINE_WAIT_S="0")
        o = fresh()
        led = me._ledger()
        pair = []
        for _ in range(2):
            t = led.new_tx(me.wallet.address, seller, 5, o["tx"]["extra"]["memo"])
            wallets.sign_tx(t, me.wallet, led)
            led._key = t["next_key"]
            pair.append(t)

        def timeout_then_other_tx():
            first = x402_pay(o["tx"], {"transaction": pair[0]})[0]  # usually 402 settlement_pending
            pending = (me.last_receipt() or {}).get("errorReason")
            other = x402_pay(o["tx"], {"transaction": pair[1]})[0]
            for _ in range(50):
                if me._node(f"/tx?id={pair[0]['txid']}")[0] == 200:
                    break
                time.sleep(0.1)
            retry = x402_pay(o["tx"], {"transaction": pair[0]})[0] if first != 200 else 200
            mined = [me._node(f"/tx?id={t['txid']}")[0] for t in pair]
            return other, {"first": first, "first_reason": pending, "retry": retry, "mined": mined}
        attempt("x402: a mining timeout, then a second tx for the same challenge", timeout_then_other_tx,
                lambda c, b: b["first_reason"] == "settlement_pending" and c == 402 and b["mined"] == [200, 404] and b["retry"] == 200)
    attempt("node still healthy afterwards", lambda: me._node("/health"), lambda c, b: c == 200)
    return results


def raw_get(node: str, path: str, headers: dict) -> int:
    u = urllib.parse.urlsplit(node)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
    try:
        conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        r.read()
        return r.status
    finally:
        conn.close()


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
        home, port = tempfile.mkdtemp(prefix="paymesh-node-"), _free_port()
        atexit.register(shutil.rmtree, home, True)  # runs after the node is killed below
        env = dict(os.environ, PAYMESH_HOME=home, PAYMESH_API_PORT=str(port), PAYMESH_EXPLORER_PORT=str(_free_port()))
        node = f"http://127.0.0.1:{port}"

        def start(**extra_env):
            procs.append(subprocess.Popen([sys.executable, os.path.join(HERE, "paymesh.py"), "serve"], env=dict(env, **extra_env),
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            for _ in range(100):
                try:
                    urllib.request.urlopen(node + "/health", timeout=1).close()
                    return
                except OSError:
                    time.sleep(0.1)

        def restart(**extra_env):
            procs[-1].kill()  # TerminateProcess: no shutdown code runs
            procs[-1].wait()
            start(**extra_env)
        start()
    try:
        res = run(node, restart)
    finally:
        for p in procs:
            p.kill()
            p.wait()  # Windows: its files are locked until it has exited
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
