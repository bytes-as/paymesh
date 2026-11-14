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
import paywall
import wallets

HOST, PORT = "127.0.0.1", 8402
CURRENCY = "PMX"
PRICES = {"/open/info": 0, "/premium/price": 5, "/premium/history": 20}
MAX_TX_BYTES = 200_000  # a signed tx is ~50 KB (Lamport pub + sig)

# simulated feed: some seeded history so /premium/history returns real data
price_history = [{"t": i, "price": round(100.0 + random.random() * 10, 4)} for i in range(12)]

chain_lock = threading.Lock()
chain_inst: chain.Chain
SELLER: wallets.Wallet


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "paymesh/0.2"

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
        with chain_lock:
            ok, reason = paywall.verify_payment(chain_inst, self.headers, path)
        if ok:
            self.paid_reason = reason
            return True
        ch = paywall.open_challenge(chain_inst, SELLER.address, price, path)
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
            self._send(200, {"status": "ok", "chain_blocks": len(chain_inst.blocks)})
            return
        if self.path.startswith("/faucet"):
            # free test faucet: mint 100 PMX to a named local wallet, mine at once
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            name = (q.get("name") or ["buyer"])[0]
            try:
                w = wallets.load_or_create(name)
            except ValueError as e:
                self._send(400, {"error": str(e)})
                return
            with chain_lock:
                chain_inst.add_tx(chain_inst.new_tx(chain.FAUCET, w.address, 100, f"faucet:{name}"))
                blk = chain_inst.mine()
            self._send(200, {"faucet": name, "address": w.address,
                             "minted": 100, "block": blk.index, "currency": CURRENCY})
            print(f"[api] faucet minted 100 PMX to '{name}' in block {blk.index}")
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
                    raise ValueError(f"body must be 1..{MAX_TX_BYTES} bytes")
                tx = json.loads(self.rfile.read(length).decode())
                with chain_lock:
                    chain_inst.add_tx(tx)  # raises on any broken ledger rule
                    blk = chain_inst.mine()
            except (ValueError, UnicodeDecodeError) as e:  # JSONDecodeError is a ValueError
                self._send(400, {"error": f"tx rejected: {e}"})
                return
            self._send(200, {"accepted": True, "txid": tx["txid"], "block": blk.index})
            print(f"[api] tx {tx['txid'][:16]}.. mined in block {blk.index}")
            return
        self._send(404, {"error": "not found"})

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start(chain_: chain.Chain, port=PORT) -> http.server.ThreadingHTTPServer:
    global chain_inst, SELLER
    chain_inst = chain_
    SELLER = wallets.load_or_create("seller")
    httpd = http.server.ThreadingHTTPServer((HOST, port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
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
