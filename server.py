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
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chain
import channels
import paywall
import wallets
import facilitator
import paymesh_x402 as x402
HOST, PORT = "127.0.0.1", int(os.environ.get("PAYMESH_API_PORT", 8402))
CURRENCY = "PMX"
PRICES = {"/open/info": 0, "/premium/price": 5, "/premium/history": 20}
MAX_TX_BYTES = 200_000  # a signed tx is ~50 KB (Lamport pub + sig)
CHANNEL_UNIT = 1  # PMX per channel tick
SETTLE_WITHIN = 20  # settle a paid channel when it expires within this many blocks
MINE_WAIT_S = float(os.environ.get("PAYMESH_MINE_WAIT_S", 15))  # how long POST /tx waits for its block
# hub payouts: pay sellers what buyers revealed on hub channels without waiting for the close,
# every HUB_PAYOUT_S seconds, or as soon as HUB_PAYOUT_MIN PMX are owed on a channel
HUB_PAYOUT_S = float(os.environ.get("PAYMESH_HUB_PAYOUT_S", 60))
HUB_PAYOUT_MIN = int(os.environ.get("PAYMESH_HUB_PAYOUT_MIN", 50))
REFUND_HOLD_S = float(os.environ.get("PAYMESH_REFUND_HOLD_S", 6.0))  # > 2 polls of a separate seller's settler

# simulated feed: some seeded history so /premium/history returns real data
price_history = [{"t": i, "price": round(100.0 + random.random() * 10, 4)} for i in range(12)]

chain_lock = threading.Lock()
mined = threading.Condition()  # notified after every block


class Stats:
    """Paid calls and revenue per route, by payment mode, since the server started."""

    def __init__(self):
        self.lock = threading.Lock()
        self.routes: dict = {}
        self.payers: set = set()
        self.started = time.time()

    def record(self, path: str, mode: str, amount: int, payer: str):
        with self.lock:
            r = self.routes.setdefault(path, {"calls": 0, "revenue": 0, "on_chain": 0, "channel": 0})
            r["calls"] += 1
            r["revenue"] += amount
            r[mode] += 1
            self.payers.add(payer)

    def view(self) -> dict:
        with self.lock:
            up = max(1e-9, time.time() - self.started)
            calls = sum(r["calls"] for r in self.routes.values())
            return {"uptime_s": round(up), "paid_calls": calls, "revenue": sum(r["revenue"] for r in self.routes.values()),
                    "calls_per_s": round(calls / up, 2), "distinct_payers": len(self.payers),
                    "routes": {p: dict(r) for p, r in sorted(self.routes.items())}}


STATS = Stats()


class RateLimit:
    """Token buckets per client address and kind of request. Over the limit: 429."""

    def __init__(self, limits: dict):
        self.limits = limits  # kind -> (burst, refill per second)
        self.buckets: dict = {}
        self.lock = threading.Lock()

    def allow(self, who: str, kind: str) -> float:
        """0 if allowed now, else seconds until a token is free."""
        burst, rate = self.limits[kind]
        now = time.monotonic()
        with self.lock:
            if len(self.buckets) > 10_000:
                self.buckets.clear()
            tokens, last = self.buckets.get((who, kind), (burst, now))
            tokens = min(burst, tokens + (now - last) * rate)
            if tokens < 1:
                self.buckets[(who, kind)] = (tokens, now)
                return (1 - tokens) / rate
            self.buckets[(who, kind)] = (tokens - 1, now)
            return 0.0


def _limit(kind, burst, rate):
    env = lambda k, d: float(os.environ.get(f"PAYMESH_{kind.upper()}_{k}", d))  # noqa: E731
    return env("BURST", burst), env("PER_S", rate)


LIMITS = RateLimit({"faucet": _limit("faucet", 200, 2), "tx": _limit("tx", 2000, 500),
                    "challenge": _limit("challenge", 5000, 1000)})
chain_inst: chain.Chain
SELLER: wallets.Wallet
BOOK: channels.SellerBook
HUB_WALLET: wallets.Wallet  # the hub: one buyer deposit, spent at any seller (see channels.SellerBook)
HUB: channels.SellerBook
FAC: "facilitator.Facilitator"  # x402 /verify, /settle, /supported for other resource servers
NETWORK = ""  # x402 network id, set by start() from the genesis hash


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "paymesh/0.3"
    disable_nagle_algorithm = True  # TCP_NODELAY: on Linux, headers then body would wait ~40 ms for an ACK
    protocol_version = "HTTP/1.1"  # keep-alive: every response carries Content-Length

    def log_message(self, fmt, *args):
        print(f"[api] {self.address_string()} {fmt % args}")

    # --- browsers: CORS on the paid routes only, other routes closed to other sites ---
    def end_headers(self):
        if self.__dict__.pop("cors", False):  # once per response; never leaks into the next request
            for k, v in x402.cors_headers():
                self.send_header(k, v)
        super().end_headers()

    def _origin_refused(self) -> bool:
        """Mark a paid route for CORS; refuse (403, True) a browser request from another site elsewhere."""
        if urllib.parse.urlsplit(self.path).path in PRICES:
            self.cors = True
            return False
        why = x402.cross_site(self.headers.get, self.headers.get("Host") or "")
        if why is None:
            return False
        if self.command == "POST":
            self._drain()
        self._send(403, {"error": f"cross-site request refused: {why}"})
        return True

    def _drain(self):
        """Read and drop a refused request's body (up to MAX_TX_BYTES) before answering. An
        unread body must not become the next request, and closing a socket with unread input
        makes Windows reset it: the client could lose the answer. A larger body: close."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if 0 <= n <= MAX_TX_BYTES:
            if n:
                self.rfile.read(n)
        else:
            self.close_connection = True

    def do_OPTIONS(self):
        """CORS preflight: answered for the paid x402 routes, refused (no CORS headers) elsewhere."""
        self.pay_headers = []
        if urllib.parse.urlsplit(self.path).path not in PRICES:
            self._send(403, {"error": "no cross-origin access to this route"})
            return
        self.send_response(204)
        for k, v in x402.preflight_headers():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- middleware wrapper: any handler can be guarded like this ---
    def _guard(self, path: str):
        """True if paid+verified, or a free route. Sends 402 itself otherwise."""
        price = PRICES.get(path)
        if price is None:
            self._send(404, {"error": "not found"})
            return False
        if price == 0:
            return True  # free route: no 402, no payment
        refused = None
        sig = self.headers.get(x402.SIGNATURE)
        if sig is not None:  # x402 v2: PAYMENT-SIGNATURE in, PAYMENT-RESPONSE out
            status, resp, method = x402.settle(sig, network=NETWORK, pay_to=SELLER.address, price=price, path=path,
                                               chain_=chain_inst, book=BOOK, submit=submit_and_wait, lock=chain_lock,
                                               hub=HUB_WALLET.address)
            self.pay_headers = [(x402.RESPONSE, x402.encode(resp))]
            if status == 200:
                self.paid_reason = f"x402 {method}: {price} PMX" + (f", tx {resp['transaction'][:16]}" if resp["transaction"] else "")
                STATS.record(path, "channel" if method == "channel" else "on_chain", price, resp.get("payer") or "?")
                return True
            if status == 400:
                self._send(400, {"error": "invalid payment", "reason": resp["errorReason"], "message": resp.get("errorMessage", "")})
                return False
            refused = resp
        chan = None if sig is not None else self.headers.get("X-Channel")
        if chan:  # paid from an open payment channel: no tx, a few hashes
            ok, reason = BOOK.accept(chain_inst, chan, price)
            if ok:
                self.paid_reason = reason
                STATS.record(path, "channel", price, chan.split()[0])
                return True
            self._send(402, {"error": "channel payment refused", "reason": reason, "amount": price, "currency": CURRENCY})
            return False
        if sig is None:  # legacy: X-Payment: <txid of a mined tx>
            with chain_lock:
                ok, reason = paywall.verify_payment(chain_inst, self.headers, path)
            if ok:
                self.paid_reason = reason
                tx, _ = chain_inst.find_tx_by_id(self.headers.get("X-Payment", "").strip())
                STATS.record(path, "on_chain", price, tx["from_wallet"] if tx else "?")
                return True
        if self._limited("challenge"):
            return False
        ch = paywall.open_challenge(chain_inst, SELLER.address, price, path)
        ch["channel"] = _terms()  # or: open a channel once and pay calls with X-Channel
        if refused:
            ch["error"] = f"payment refused: {refused.get('errorMessage', refused['errorReason'])}"
        host = self.headers.get("Host") or f"{HOST}:{PORT}"
        required = x402.payment_required(f"http://{host}{path}", x402.paymesh_accepts(NETWORK, SELLER.address, price, ch, CHANNEL_UNIT,
                                                                                     hub=HUB_WALLET.address,
                                                                                     height=chain_inst.height),
                                         error=ch["error"] if refused else "PAYMENT-SIGNATURE header is required")
        body = json.dumps(ch).encode()
        self.send_response(402)  # Payment Required
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Payment-Challenge", "true")
        self.send_header(x402.REQUIRED, x402.encode(required))
        for k, v in self.pay_headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        print(f"[api] 402 challenged {path} nonce={ch['nonce'][:12]} amount={price}")
        return False

    def do_GET(self):
        self.pay_headers = []  # x402 headers for this response (the handler lives across keep-alive requests)
        if self._origin_refused():
            return
        if self.path.startswith("/health"):
            if chain_inst.corrupt:
                self._send(503, {"status": "integrity check failed", "reason": chain_inst.corrupt})
            else:
                self._send(200, {"status": "ok", "chain_blocks": len(chain_inst.blocks), "network": NETWORK})
            return
        if self.path.startswith("/faucet") and self._limited("faucet"):
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
        if self.path.startswith("/tx?") or self.path.startswith("/channel/info"):
            # read-only lookups for seller services running apart from the node
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            key = (q.get("id") or [""])[0]
            if self.path.startswith("/tx?"):
                tx, blk = chain_inst.find_tx_by_id(key)
                if tx is None:
                    self._send(404, {"error": "no mined tx with that id"})
                else:
                    self._send(200, {"tx": {k: v for k, v in tx.items() if k not in ("sig", "pub")}, "block": blk.index})
            else:
                with chain_lock:
                    c = chain_inst.ledger.channels.get(key)
                    c = dict(c) if c else None
                self._send(200, c) if c else self._send(404, {"error": "unknown channel"})
            return
        if self.path.startswith("/stats"):
            with chain_lock:
                chain_stats = {"fees_burned": chain_inst.ledger.burned, "chain_bytes": os.path.getsize(chain_inst.path),
                               "blocks": chain_inst.height}
            self._send(200, dict(STATS.view(), chain=chain_stats))
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
            b = _book_for(c)[0].best(cid) or {"i": 0}
            self._send(200, {"channel": cid, "open": c["open"], "ticks": c["n"], "unit": c["unit"],
                             "paid_ticks": b["i"], "expires": c["expires"]})
            return
        if self.path.startswith("/channel/terms"):
            self._send(200, _terms())
            return
        if self.path.split("?")[0] == "/supported":
            self._send(200, FAC.supported())
            return
        if self.path.startswith("/hub/terms"):
            self._send(200, {"hub": HUB_WALLET.address, "unit": CHANNEL_UNIT, "currency": CURRENCY,
                             "max_ticks": chain.MAX_TICKS, "open": "POST /tx a signed channel_open to the hub",
                             "claim": "POST /hub/claim {channel, tick, word, seller, price} (sellers)",
                             "close": "POST /channel/close {channel}: one channel_close pays every seller (splits)"})
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
        self.pay_headers = []
        if self._origin_refused():
            return
        if self.path.startswith("/tx") and self._limited("tx"):
            return
        if self.path.startswith("/tx"):
            # agents submit a signed tx; the server (sole miner) verifies and mines it
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= MAX_TX_BYTES:
                    self.close_connection = True  # the unread body must not become the next request
                    raise ValueError(f"body must be 1..{MAX_TX_BYTES} bytes")
                tx = x402.loads(self.rfile.read(length).decode())
                blk = submit_and_wait(tx)
            except (ValueError, UnicodeDecodeError) as e:  # JSONDecodeError is a ValueError
                self._send(400, {"error": f"tx rejected: {e}"})
                return
            self._send(200, {"accepted": True, "txid": tx["txid"], "block": blk.index})
            print(f"[api] tx {tx['txid'][:16]}.. mined in block {blk.index}")
            return
        if self.path.split("?")[0] in ("/verify", "/settle"):
            # x402 facilitator: answer 200 with a VerifyResponse / SettlementResponse either way
            settle_call = self.path.startswith("/settle")
            if self._limited("tx" if settle_call else "challenge"):
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= MAX_TX_BYTES:
                    self.close_connection = True
                    raise ValueError(f"body must be 1..{MAX_TX_BYTES} bytes")
                body = x402.loads(self.rfile.read(length).decode())
            except (ValueError, UnicodeDecodeError) as e:
                self._send(400, {"error": f"facilitator request refused: {e}"})
                return
            try:
                res = FAC.settle(body) if settle_call else FAC.verify(body)
            except Exception as e:  # noqa: BLE001 - odd input must never kill the request
                print(f"[api] facilitator {type(e).__name__}: {e}")
                res = (x402.settlement(NETWORK, False, reason="unexpected_settle_error", message="malformed")
                       if settle_call else {"isValid": False, "invalidReason": "unexpected_verify_error"})
            if settle_call and res.get("success"):
                STATS.record("facilitator", "on_chain" if res["transaction"] else "channel",
                             int(res.get("amount", 0)), res.get("payer") or "?")
            self._send(200, res)
            return
        if self.path.startswith("/hub/claim"):
            # a seller hands the hub a word a buyer paid it with; the hub credits that seller
            if self._limited("challenge"):
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 4096:
                    self.close_connection = True
                    raise ValueError("body must be 1..4096 bytes")
                q = x402.loads(self.rfile.read(length).decode())
                if not isinstance(q, dict):
                    raise ValueError("body must be an object")
                cid, tick, word, payee, price = (q.get(k) for k in ("channel", "tick", "word", "seller", "price"))
                if not (isinstance(cid, str) and type(tick) is int and tick > 0 and isinstance(word, str)
                        and isinstance(payee, str) and type(price) is int and price > 0):
                    raise ValueError("claim is {channel, tick: int > 0, word, seller, price: int > 0}")
            except (ValueError, UnicodeDecodeError) as e:
                self._send(400, {"error": f"claim refused: {e}"})
                return
            ok, why = HUB.accept(chain_inst, f"{cid} {tick} {word}", price, payee=payee)
            if not ok:
                self._send(402, {"error": "hub claim refused", "reason": why})
                return
            self._send(200, {"ok": True, "reason": why, "seller": payee})
            return
        if self.path.startswith("/channel/close"):
            # settle a channel at the best word the seller holds (anyone may ask: it
            # can only pay the seller what the buyer revealed, the rest goes back)
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 4096:
                    self.close_connection = True
                    raise ValueError("body must be 1..4096 bytes")
                cid = str(x402.loads(self.rfile.read(length).decode()).get("channel", ""))
                blk, tx = settle(cid)
            except (ValueError, UnicodeDecodeError, AttributeError) as e:
                self._send(400, {"error": f"close refused: {e}"})
                return
            self._send(200, {"settled": True, "channel": cid, "paid_to_seller": tx["amount"], "block": blk.index})
            return
        self._send(404, {"error": "not found"})

    def _limited(self, kind: str) -> bool:
        """Send 429 and return True when this client is over its `kind` limit."""
        wait = LIMITS.allow(self.client_address[0], kind)
        if not wait:
            return False
        if self.command == "POST":
            self._drain()  # before the answer: see _drain
        body = json.dumps({"error": "too many requests", "kind": kind, "retry_after_s": round(wait, 2)}).encode()
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", str(max(1, int(wait + 0.999))))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in getattr(self, "pay_headers", ()):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


def _terms() -> dict:
    return {"seller": SELLER.address, "hub": HUB_WALLET.address, "unit": CHANNEL_UNIT, "currency": CURRENCY,
            "max_ticks": chain.MAX_TICKS, "open": "POST /tx a signed channel_open (see channels.py)",
            "pay": "X-Channel: <channel id> <tick> <word>", "close": "POST /channel/close {channel}"}


def submit_and_wait(tx) -> "chain.Block":
    """Put a signed tx on chain and wait until it is mined. ValueError if any rule refuses it."""
    if not isinstance(tx, dict):
        raise ValueError("a tx is a JSON object")
    if tx.get("from_wallet") == chain.FAUCET:
        raise ValueError("faucet mints come from GET /faucet only")
    cid = tx.get("channel")
    c = chain_inst.ledger.channels.get(cid) if tx.get("kind") == "channel_refund" and isinstance(cid, str) else None
    ours = c is not None and c["seller"] in (SELLER.address, HUB_WALLET.address)
    if c and c["open"] and ours and _book_for(c)[0].best(cid):
        # this node is the seller (or the hub) and the miner: settle what the buyer spent
        # before a refund can take it back
        try:
            settle(cid)
        except ValueError as e:
            print(f"[api] settle {cid[:12]} before refund failed: {e}")
    elif c and c["open"] and not ours:
        # a seller running apart can't front-run (blocks are cheap: a buyer can mine past its
        # 2 s settler). Hold the refund so it can settle first; whichever lands first counts.
        deadline = time.monotonic() + REFUND_HOLD_S
        while time.monotonic() < deadline and chain_inst.ledger.channels.get(cid, {}).get("open"):
            with mined:
                mined.wait(0.1)
    try:
        with chain_lock:
            chain_inst.add_tx(tx)  # raises on any broken ledger rule
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001 - a malformed tx is refused, never kills the request
        print(f"[api] unexpected {type(e).__name__} on a tx: {e}")
        raise ValueError("malformed") from None
    # the miner thread puts every waiting tx into its next block
    with mined:
        mined.notify_all()
        if not mined.wait_for(lambda: tx["txid"] in chain_inst.where, timeout=MINE_WAIT_S):
            raise x402.Pending(x402.PENDING_MSG)
    return chain_inst.blocks[chain_inst.where[tx["txid"]]]


def _book_for(c) -> tuple:
    """(book, signing wallet) for a channel: the hub's or this node's own seller's."""
    return (HUB, HUB_WALLET) if c and c["seller"] == HUB_WALLET.address else (BOOK, SELLER)


def _hub_claim(payee: str):
    """For a seller's book: take words of hub channels through the hub, crediting `payee`."""
    def claim(channel_seller, header, price):
        if channel_seller != HUB_WALLET.address:
            return None
        return HUB.accept(chain_inst, header, price, payee=payee)
    return claim


def settle(cid: str):
    with chain_lock:
        book, wallet = _book_for(chain_inst.ledger.channels.get(cid))
        tx = book.close_tx(chain_inst, wallet, cid)
        chain_inst.add_tx(tx)
        blk = chain_inst.mine()
    with mined:
        mined.notify_all()
    print(f"[api] channel {cid[:12]} settled: {tx['amount']} PMX to seller in block {blk.index}")
    return blk, tx


def _crash_point(name: str):
    """Fault injection for the crash tests: PAYMESH_CRASH_AT=<name> kills the node the first
    time it reaches <name> (a marker file next to the chain makes it once per data folder)."""
    if os.environ.get("PAYMESH_CRASH_AT") != name:
        return
    marker = os.path.join(os.path.dirname(os.path.abspath(chain_inst.path)), f"crashed-at-{name}")
    if not os.path.exists(marker):
        open(marker, "w").close()
        os._exit(70)  # no cleanup, like a power cut of the process


def payout(cid: str):
    """Pay a hub channel's sellers what was revealed since the last payout (channel stays open)."""
    with chain_lock:
        tx = HUB.payout_tx(chain_inst, HUB_WALLET, cid)
        _crash_point("payout_signed")
        chain_inst.add_tx(tx)
        _crash_point("payout_pending")
        blk = chain_inst.mine()
        _crash_point("payout_mined")
    with mined:
        mined.notify_all()
    print(f"[api] hub payout on {cid[:12]}: {tx['amount']} PMX to {len(tx['splits'])} seller(s) in block {blk.index}")
    return blk, tx


_owed_since: dict = {}  # hub channel -> when its oldest unpaid tick was first seen


def _payouts_due(now: float) -> list:
    owed = HUB.unpaid(chain_inst)
    for cid in list(_owed_since):
        if cid not in owed:
            del _owed_since[cid]
    due = []
    expiring = set(HUB.expiring(chain_inst, SETTLE_WITHIN))  # those get closed instead
    for cid, amount in owed.items():
        since = _owed_since.setdefault(cid, now)
        if cid not in expiring and (amount >= HUB_PAYOUT_MIN or (now - since >= HUB_PAYOUT_S and amount >= 2)):
            due.append(cid)
    return due


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
    """Settle paid channels before they expire, so the buyer can't refund what it spent, and
    pay hub sellers on schedule (HUB_PAYOUT_S / HUB_PAYOUT_MIN)."""
    while not stop.wait(min(2.0, max(0.2, HUB_PAYOUT_S / 4))):
        for cid in BOOK.expiring(chain_inst, SETTLE_WITHIN) + HUB.expiring(chain_inst, SETTLE_WITHIN):
            try:
                settle(cid)
            except ValueError as e:
                print(f"[api] settle {cid[:12]} failed: {e}")
        for cid in _payouts_due(time.time()):
            try:
                payout(cid)
                _owed_since.pop(cid, None)
            except ValueError as e:
                print(f"[api] payout {cid[:12]} failed: {e}")


def start(chain_: chain.Chain, port=PORT) -> http.server.ThreadingHTTPServer:
    global chain_inst, SELLER, BOOK, NETWORK, HUB_WALLET, HUB, FAC
    chain_inst = chain_
    NETWORK = x402.network_id(chain_.blocks[0].hash())
    SELLER = wallets.load_or_create("seller")
    home = os.path.dirname(os.path.abspath(chain_.path))
    BOOK = channels.SellerBook(os.path.join(home, "seller_channels.json"), SELLER.address)
    HUB_WALLET = wallets.load_or_create("hub")
    HUB = channels.SellerBook(os.path.join(home, "hub_channels.log"), HUB_WALLET.address)
    BOOK.hub = _hub_claim(SELLER.address)  # this node's own API also takes hub channels
    FAC = facilitator.Facilitator(chain_, chain_lock, lambda tx: submit_and_wait(tx), HUB, HUB_WALLET.address,
                                  NETWORK, os.path.join(home, "facilitator_settled.log"))
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
