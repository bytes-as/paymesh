"""paymesh demo paid API on localhost:8402, with a per-endpoint price table.

Price map (PMX per call):
    GET /open/info      = 0   (free, no 402 ever)
    GET /premium/price  = 5
    GET /premium/history = 20 (the price feed's own recorded history)

Unauthenticated paid requests get 402 + a challenge quoting that route's
exact amount; payments are verified against it. Stdlib only; run:
    py -3 server.py
"""
import http.server
import json
import os
import random
import sys
import threading
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chain
import channels
import paywall
import wallets

HOST, PORT = "127.0.0.1", int(os.environ.get("PAYMESH_API_PORT", 8402))
CURRENCY = "PMX"
PRICES = {"/open/info": 0, "/premium/price": 5, "/premium/history": 20}
MAX_TX_BYTES = 200_000  # a signed tx is ~50 KB (Lamport pub + sig)
CHANNEL_UNIT = 1  # PMX per channel tick
SETTLE_WITHIN = 20  # settle a paid channel when it expires within this many blocks

# simulated feed: some seeded history so /premium/history returns real data
price_history = [{"t": i, "price": round(100.0 + random.random() * 10, 4)} for i in range(12)]

chain_lock = threading.Lock()
mined = threading.Condition()  # notified after every block
chain_inst: chain.Chain
SELLER: wallets.Wallet
BOOK: channels.SellerBook


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "paymesh/0.3"
    protocol_version = "HTTP/1.1"  # keep-alive: every response carries Content-Length

    def log_message(self, fmt, *args):
        print(f"[api] {self.address_string()} {fmt % args}")

    # --- middleware wrapper: any handler can be guarded like this ---
    def _guard(self, path: str):
        """True if paid+verified, or a free route. Sends 402 itself otherwise."""
        price = PRICES.get(path)
        if price is None:
            self._send(404, {"error": "not found"})
            return False
        if price == 0:
            return True  # free route: no 402, no payment
        chan = self.headers.get("X-Channel")
        if chan:  # paid from an open payment channel: no tx, a few hashes
            ok, reason = BOOK.accept(chain_inst, chan, price)
            if ok:
                self.paid_reason = reason
                return True
            self._send(402, {"error": "channel payment refused", "reason": reason, "amount": price, "currency": CURRENCY})
            return False
        with chain_lock:
            ok, reason = paywall.verify_payment(chain_inst, self.headers, path)
        if ok:
            self.paid_reason = reason
            return True
        ch = paywall.open_challenge(chain_inst, SELLER.address, price, path)
        ch["channel"] = _terms()  # or: open a channel once and pay calls with X-Channel
        body = json.dumps(ch).encode()
        self.send_response(402)  # Payment Required
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Payment-Challenge", "true")
        self.end_headers()
        self.wfile.write(body)
        print(f"[api] 402 challenged {path} nonce={ch['nonce'][:12]} amount={price}")
        return False

    def do_GET(self):
        if self.path.startswith("/health"):
            if chain_inst.corrupt:
                self._send(503, {"status": "integrity check failed", "reason": chain_inst.corrupt})
            else:
                self._send(200, {"status": "ok", "chain_blocks": len(chain_inst.blocks)})
            return
        if self.path.startswith("/faucet"):
            # free test faucet: mint 100 PMX to a named local wallet, mine at once
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            # ?address=<64 hex> mints to any wallet (clients keep their own keys);
            # ?name= is the demo shortcut that creates a wallet on this machine
            addr = (q.get("address") or [""])[0].lower()
            name = (q.get("name") or ["buyer"])[0]
            if addr:
                if not chain.HEX64.match(addr):
                    self._send(400, {"error": "address must be 64 hex"})
                    return
                name = addr[:12]
            else:
                try:
                    addr = wallets.load_or_create(name).address
                except ValueError as e:
                    self._send(400, {"error": str(e)})
                    return
            with chain_lock:
                chain_inst.add_tx(chain_inst.new_tx(chain.FAUCET, addr, 100, f"faucet:{name}"))
                blk = chain_inst.mine()
            with mined:
                mined.notify_all()
            self._send(200, {"faucet": name, "address": addr,
                             "minted": 100, "block": blk.index, "currency": CURRENCY})
            print(f"[api] faucet minted 100 PMX to '{name}' in block {blk.index}")
            return
        if self.path.startswith("/account"):
            # what a client needs to sign its next tx, without reading chain.json
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            addr = (q.get("address") or [""])[0].lower()
            if not chain.HEX64.match(addr):
                self._send(400, {"error": "address must be 64 hex"})
                return
            with chain_lock:
                info = {"address": addr, "balance": chain_inst.balance(addr), "current_key": chain_inst.current_key(addr),
                        "height": chain_inst.height,
                        "channels": {k: v for k, v in chain_inst.ledger.channels.items() if v["buyer"] == addr and v["open"]}}
            self._send(200, info)
            return
        if self.path.startswith("/channel/state"):
            # the last tick this seller accepted: a restarted buyer resumes from it
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            cid = (q.get("channel") or [""])[0]
            with chain_lock:
                c = chain_inst.ledger.channels.get(cid)
            if c is None:
                self._send(404, {"error": "unknown channel"})
                return
            b = BOOK.best(cid) or {"i": 0}
            self._send(200, {"channel": cid, "open": c["open"], "ticks": c["n"], "unit": c["unit"],
                             "paid_ticks": b["i"], "expires": c["expires"]})
            return
        if self.path.startswith("/channel/terms"):
            self._send(200, _terms())
            return
        if self.path.startswith("/open/info"):
            self._send(200, {"service": "paymesh demo API", "currency": CURRENCY,
                             "endpoints": PRICES})
            return
        if self.path.startswith("/premium/price"):
            if not self._guard("/premium/price"):
                return
            quote = round(100.0 + random.random() * 10, 4)  # made-up live feed
            price_history.append({"t": len(price_history), "price": quote})
            self._send(200, {"asset": "SYNTH/USD", "price": quote,
                             "currency": CURRENCY, "paid": self.paid_reason})
            return
        if self.path.startswith("/premium/history"):
            if not self._guard("/premium/history"):
                return
            self._send(200, {"asset": "SYNTH/USD", "points": price_history,
                             "count": len(price_history), "currency": CURRENCY,
                             "paid": self.paid_reason})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.startswith("/tx"):
            # agents submit a signed tx; the server (sole miner) verifies and mines it
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= MAX_TX_BYTES:
                    self.close_connection = True  # the unread body must not become the next request
                    raise ValueError(f"body must be 1..{MAX_TX_BYTES} bytes")
                tx = json.loads(self.rfile.read(length).decode())
                if isinstance(tx, dict) and tx.get("from_wallet") == chain.FAUCET:
                    raise ValueError("faucet mints come from GET /faucet only")
                with chain_lock:
                    chain_inst.add_tx(tx)  # raises on any broken ledger rule
                # the miner thread puts every waiting tx into its next block
                with mined:
                    mined.notify_all()
                    if not mined.wait_for(lambda: tx["txid"] in chain_inst.where, timeout=15):
                        raise ValueError("not mined in time; it stays pending")
                blk = chain_inst.blocks[chain_inst.where[tx["txid"]]]
            except (ValueError, UnicodeDecodeError) as e:  # JSONDecodeError is a ValueError
                self._send(400, {"error": f"tx rejected: {e}"})
                return
            except Exception as e:  # noqa: BLE001 - a malformed tx must get 400, never kill the request
                print(f"[api] unexpected {type(e).__name__} on /tx: {e}")
                self._send(400, {"error": "tx rejected: malformed"})
                return
            self._send(200, {"accepted": True, "txid": tx["txid"], "block": blk.index})
            print(f"[api] tx {tx['txid'][:16]}.. mined in block {blk.index}")
            return
        if self.path.startswith("/channel/close"):
            # settle a channel at the best word the seller holds (anyone may ask: it
            # can only pay the seller what the buyer revealed, the rest goes back)
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 4096:
                    self.close_connection = True
                    raise ValueError("body must be 1..4096 bytes")
                cid = str(json.loads(self.rfile.read(length).decode()).get("channel", ""))
                blk, tx = settle(cid)
            except (ValueError, UnicodeDecodeError, AttributeError) as e:
                self._send(400, {"error": f"close refused: {e}"})
                return
            self._send(200, {"settled": True, "channel": cid, "paid_to_seller": tx["amount"], "block": blk.index})
            return
        self._send(404, {"error": "not found"})

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _terms() -> dict:
    return {"seller": SELLER.address, "unit": CHANNEL_UNIT, "currency": CURRENCY,
            "max_ticks": chain.MAX_TICKS, "open": "POST /tx a signed channel_open (see channels.py)",
            "pay": "X-Channel: <channel id> <tick> <word>", "close": "POST /channel/close {channel}"}


def settle(cid: str):
    with chain_lock:
        tx = BOOK.close_tx(chain_inst, SELLER, cid)
        chain_inst.add_tx(tx)
        blk = chain_inst.mine()
    with mined:
        mined.notify_all()
    print(f"[api] channel {cid[:12]} settled: {tx['amount']} PMX to seller in block {blk.index}")
    return blk, tx


def _miner(stop: threading.Event):
    """Mine whatever is pending. While one block is mined, new txs queue for the next,
    so under load many txs share a block instead of each paying for its own."""
    while not stop.is_set():
        with mined:
            mined.wait_for(lambda: chain_inst.pending or stop.is_set(), timeout=1.0)
        if stop.is_set():
            break
        with chain_lock:
            if chain_inst.pending:
                chain_inst.mine()
        with mined:
            mined.notify_all()


def _settler(stop: threading.Event):
    """Settle paid channels before they expire, so the buyer can't refund what it spent."""
    while not stop.wait(2.0):
        for cid in BOOK.expiring(chain_inst, SETTLE_WITHIN):
            try:
                settle(cid)
            except ValueError as e:
                print(f"[api] settle {cid[:12]} failed: {e}")


def start(chain_: chain.Chain, port=PORT) -> http.server.ThreadingHTTPServer:
    global chain_inst, SELLER, BOOK
    chain_inst = chain_
    SELLER = wallets.load_or_create("seller")
    BOOK = channels.SellerBook(os.path.join(os.path.dirname(os.path.abspath(chain_.path)), "seller_channels.json"), SELLER.address)
    httpd = http.server.ThreadingHTTPServer((HOST, port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    # a fast start trusted the checkpoint: re-check those blocks in full meanwhile
    threading.Thread(target=chain_.verify_checkpoint, daemon=True).start()
    httpd.settler_stop = threading.Event()
    threading.Thread(target=_settler, args=(httpd.settler_stop,), daemon=True).start()
    threading.Thread(target=_miner, args=(httpd.settler_stop,), daemon=True).start()
    routes = "  ".join(f"{p}={v} PMX" for p, v in PRICES.items())
    print(f"[api] paymesh API on http://{HOST}:{port}  (prices: {routes})")
    return httpd


if __name__ == "__main__":
    c = chain.Chain.open_or_archive()
    httpd = start(c)
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        httpd.shutdown()
