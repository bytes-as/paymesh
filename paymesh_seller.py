"""paymesh for sellers: put a price on any HTTP API, running apart from the node.

    from paymesh_seller import Paywall
    paywall = Paywall(node="http://127.0.0.1:8402", wallet="my-shop",
                      prices={"/forecast": 3})
    app = paywall.wsgi(my_wsgi_app)        # unpaid calls get 402, paid ones pass through

or, in any framework:  ok, status, body = paywall.check(path, headers)

The seller keeps its own wallet (it signs channel settlements), its own open
challenges and its own channel log; it asks the node only to look up mined
txs and channels. It also serves /channel/terms, /channel/state and
/channel/close for buyers, and settles channels close to expiry by itself.
Stdlib only.
"""
import http.client
import json
import os
import threading
import time
import urllib.parse

import channels
import httppool
import paywall as _pw
import wallets
import paymesh_x402 as x402
class _Node:
    """Just enough of a chain for paywall.py and channels.py, read from the node over HTTP."""

    CHANNEL_TTL = 2.0  # seconds a channel's open/settled state is cached

    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.lock = threading.RLock()
        self.pool = httppool.Pool(self.url, timeout)  # kept-alive connections, shared by all threads
        self._channels: dict = {}
        self.ledger = self  # channels.py reads chain.ledger.channels
        self.channels = self

    def _get(self, path):
        status, _, body = self.pool.request("GET", path)
        return status, json.loads(body.decode() or "null")

    def close(self):
        self.pool.close()

    def post(self, path, obj, keep_alive: bool = False):
        """keep_alive: reuse this thread's connection (hub claims, one per paid call). A reused
        connection the node closed while idle is retried once on a new one; that request never
        reached the node. Txs keep a fresh connection: never sent twice."""
        u = urllib.parse.urlsplit(self.url)
        if keep_alive:
            status, _, body = self.pool.request("POST", path, json.dumps(obj).encode(), {"Content-Type": "application/json"})
            return status, json.loads(body.decode() or "null")
        c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=self.timeout)
        try:
            c.request("POST", u.path + path, body=json.dumps(obj), headers={"Content-Type": "application/json"})
            r = c.getresponse()
            return r.status, json.loads(r.read().decode() or "null")
        finally:
            c.close()

    @property
    def network(self) -> str:
        """The node's x402 network id (from its genesis hash), asked once."""
        if not getattr(self, "_network", ""):
            code, body = self._get("/health")
            if code != 200 or not isinstance(body, dict) or not body.get("network"):
                raise ValueError(f"node has no x402 network id: {body}")
            self._network = body["network"]
        return self._network

    @property
    def height(self) -> int:
        """The node's chain height, cached CHANNEL_TTL seconds (channel ticks stop near expiry)."""
        hit = getattr(self, "_height", None)
        if hit and time.monotonic() - hit[0] < self.CHANNEL_TTL:
            return hit[1]
        code, body = self._get("/health")
        if code != 200 or not isinstance(body, dict) or not isinstance(body.get("chain_blocks"), int):
            raise ValueError(f"node height unknown: {body}")
        self._height = (time.monotonic(), body["chain_blocks"])
        return body["chain_blocks"]

    def submit(self, tx):
        """x402 settlement: put the buyer's signed tx on chain through the node."""
        code, res = self.post("/tx", tx)
        if code != 200:
            msg = str((res or {}).get("error", res)) if isinstance(res, dict) else str(res)
            if x402.PENDING_MSG in msg:  # accepted by the node, not yet mined: not a refusal
                raise x402.Pending(msg)
            raise ValueError(msg)
        return res

    # chain-like API used by paywall.verify_payment
    @property
    def tip_hash(self) -> str:
        return ""

    def find_tx_by_id(self, txid):
        code, body = self._get(f"/tx?id={urllib.parse.quote(txid)}")
        if code != 200:
            return None, None
        blk = type("Block", (), {"index": body["block"]})()
        return body["tx"], blk

    # dict-like ledger.channels used by channels.SellerBook
    def get(self, cid, default=None):
        hit = self._channels.get(cid)
        if hit and time.monotonic() - hit[0] < self.CHANNEL_TTL:
            return hit[1]
        code, body = self._get(f"/channel/info?id={urllib.parse.quote(str(cid))}")
        val = body if code == 200 else None
        self._channels[cid] = (time.monotonic(), val)
        return val if val is not None else default

    def forget(self, cid):
        self._channels.pop(cid, None)

    def account(self, address):
        code, body = self._get(f"/account?address={address}")
        if code != 200:
            raise ValueError(f"node: {body}")
        return body

    @property
    def hub(self) -> str:
        """The node's hub address ("" if it runs none), asked once."""
        if getattr(self, "_hub", None) is None:
            try:
                code, body = self._get("/hub/terms")
                self._hub = body["hub"] if code == 200 and isinstance(body, dict) else ""
            except (OSError, KeyError, ValueError):
                return ""  # ask again next time
        return self._hub


class _Signer:
    """A ledger stand-in for wallets.sign_tx: the node says which key signs next."""

    def __init__(self, node: _Node, address: str):
        self.info = node.account(address)
        self.height = self.info["height"]

    def current_key(self, address):
        return self.info["current_key"]


class Paywall:
    def __init__(self, node: str, wallet: str, prices: dict, unit: int = 1, book_path: str | None = None,
                 settle_within: int = 20):
        self.node = _Node(node)
        self.wallet = wallets.load_or_create(wallet)
        self.prices = dict(prices)
        self.unit = unit
        self.book = channels.SellerBook(book_path or os.path.join(wallets.WALLET_DIR, f"{self.wallet.name}.channels.log"),
                                        self.wallet.address)
        self.book.hub = self._hub_claim  # words of the node's hub channels go to the hub, credited to us
        self.settle_within = settle_within
        self.stop = threading.Event()
        threading.Thread(target=self._settler, daemon=True).start()

    def _hub_claim(self, channel_seller: str, header: str, price: int):
        """A word from a channel opened to the node's hub: the hub takes it and credits this
        seller; it pays us in the channel_close (splits). None: not a hub channel."""
        if not channel_seller or channel_seller != self.node.hub:
            return None
        cid, tick, word = header.split()
        try:
            code, res = self.node.post("/hub/claim", {"channel": cid, "tick": int(tick), "word": word,
                                                      "seller": self.wallet.address, "price": price}, keep_alive=True)
        except OSError as e:
            return False, f"hub unreachable: {e}"
        if code == 200:
            return True, (res or {}).get("reason", "paid through the hub")
        return False, f"hub: {(res or {}).get('reason') or res}"

    # --- the check, framework-agnostic ---
    def terms(self) -> dict:
        return {"seller": self.wallet.address, "unit": self.unit, "currency": "PMX",
                "pay": "X-Channel: <channel id> <tick> <word>", "close": "POST /channel/close {channel}"}

    def check(self, path: str, headers) -> tuple[bool, int, dict | None]:
        """(True, 200, None) if the call may proceed; else (False, status, json body to send).
        Drops the x402 response headers: use gate() to send them."""
        ok, status, body, _ = self.gate(path, headers)
        return ok, status, body

    def gate(self, path: str, headers, url: str = "") -> tuple[bool, int, dict | None, list]:
        """Like check(), plus the headers to send: PAYMENT-REQUIRED on a 402, PAYMENT-RESPONSE
        once an x402 payment was tried. `url` is the resource URL quoted in PAYMENT-REQUIRED."""
        price = self.prices.get(path)
        if price is None or price == 0:
            return True, 200, None, []
        out, refused = [], None
        sig = headers.get(x402.SIGNATURE)
        if sig is not None:
            status, resp, _ = x402.settle(sig, network=self.node.network, pay_to=self.wallet.address, price=price,
                                          path=path, chain_=self.node, book=self.book, submit=self.node.submit, hub=self.node.hub)
            out = [(x402.RESPONSE, x402.encode(resp))]
            if status == 200:
                return True, 200, None, out
            if status == 400:
                return False, 400, {"error": "invalid payment", "reason": resp["errorReason"],
                                    "message": resp.get("errorMessage", "")}, out
            refused = resp
        else:
            chan = headers.get("X-Channel")
            if chan:
                ok, reason = self.book.accept(self.node, chan, price)
                if ok:
                    return True, 200, None, []
                return False, 402, {"error": "channel payment refused", "reason": reason, "amount": price, "currency": "PMX"}, []
            if headers.get("X-Payment"):
                ok, reason = _pw.verify_payment(self.node, headers, path)
                if ok:
                    return True, 200, None, []
        body = _pw.open_challenge(self.node, self.wallet.address, price, path)
        body["channel"] = self.terms()
        body["node"] = self.node.url  # where to send the tx
        if refused:
            body["error"] = f"payment refused: {refused.get('errorMessage', refused['errorReason'])}"
        required = x402.payment_required(url or path, x402.paymesh_accepts(self.node.network, self.wallet.address, price,
                                                                             body, self.unit, node=self.node.url,
                                                                             hub=self.node.hub, height=self.node.height),
                                         error=body["error"] if refused else "PAYMENT-SIGNATURE header is required")
        return False, 402, body, out + [(x402.REQUIRED, x402.encode(required))]

    def close(self):
        """Stop the settler thread and close the connections to the node."""
        self.stop.set()
        self.node.close()

    def settle(self, cid: str) -> dict:
        signer = _Signer(self.node, self.wallet.address)
        c = self.node.get(cid)
        b = self.book.best(cid)
        if c is None or not c["open"]:
            raise ValueError("no open channel with that id")
        if not b:
            raise ValueError("nothing paid on this channel yet")
        tx = signer_tx = _new_close(self.wallet.address, c, cid, b)
        wallets.sign_tx(signer_tx, self.wallet, signer)
        code, res = self.node.post("/tx", tx)
        if code != 200:
            raise ValueError(f"node refused the settlement: {res}")
        self.node.forget(cid)
        return {"settled": True, "channel": cid, "paid_to_seller": tx["amount"], "block": res["block"]}

    def _settler(self):
        while not self.stop.wait(2.0):
            for cid, b in list(self.book.book.items()):
                c = self.node.get(cid)
                if c and c["open"]:
                    try:
                        h = self.node.account(self.wallet.address)["height"]
                        if c["expires"] - h <= self.settle_within:
                            self.settle(cid)
                    except (ValueError, OSError):
                        pass

    # --- WSGI ---
    def wsgi(self, app):
        def wrapped(environ, start_response):
            path = environ.get("PATH_INFO", "/")
            if path in self.prices:  # browsers: open CORS on paid routes (x402 headers exposed)
                if environ.get("REQUEST_METHOD") == "OPTIONS":
                    start_response("204 No Content", x402.preflight_headers() + [("Content-Length", "0")])
                    return [b""]
                plain_start = start_response

                def start_response(status_line, hdrs, exc_info=None):
                    return plain_start(status_line, list(hdrs) + x402.cors_headers(), exc_info)
            elif path in ("/channel/terms", "/channel/state", "/channel/close"):
                why = x402.cross_site(lambda k: environ.get("HTTP_" + k.upper().replace("-", "_")),
                                      environ.get("HTTP_HOST", ""))
                if why:
                    try:  # read a small body first: closing on unread input resets the connection
                        n = int(environ.get("CONTENT_LENGTH") or 0)
                        if 0 < n <= 4096:
                            environ["wsgi.input"].read(n)
                    except (ValueError, OSError):
                        pass
                    return _json(start_response, 403, {"error": f"cross-site request refused: {why}"})
            headers = {"X-Channel": environ.get("HTTP_X_CHANNEL"), "X-Payment": environ.get("HTTP_X_PAYMENT"),
                       x402.SIGNATURE: environ.get("HTTP_PAYMENT_SIGNATURE")}
            headers = {k: v for k, v in headers.items() if v is not None and (v or k == x402.SIGNATURE)}
            if path == "/channel/terms":
                return _json(start_response, 200, self.terms())
            if path == "/channel/state":
                cid = (urllib.parse.parse_qs(environ.get("QUERY_STRING", "")).get("channel") or [""])[0]
                c = self.node.get(cid)
                if c is None:
                    return _json(start_response, 404, {"error": "unknown channel"})
                return _json(start_response, 200, {"channel": cid, "open": c["open"], "ticks": c["n"], "unit": c["unit"],
                                                   "paid_ticks": (self.book.best(cid) or {"i": 0})["i"], "expires": c["expires"]})
            if path == "/channel/close" and environ.get("REQUEST_METHOD") == "POST":
                try:
                    n = int(environ.get("CONTENT_LENGTH") or 0)
                    if not 0 < n <= 4096:
                        raise ValueError("body must be 1..4096 bytes")
                    cid = str(x402.loads(environ["wsgi.input"].read(n)).get("channel", ""))
                    return _json(start_response, 200, self.settle(cid))
                except (ValueError, AttributeError) as e:
                    return _json(start_response, 400, {"error": f"close refused: {e}"})
            url = f"{environ.get('wsgi.url_scheme', 'http')}://{environ.get('HTTP_HOST', 'localhost')}{path}"
            ok, status, body, extra = self.gate(path, _H(headers), url)
            if not ok:
                return _json(start_response, status, body, extra)
            if not extra:
                return app(environ, start_response)

            def start_with_receipt(status_line, hdrs, exc_info=None):  # PAYMENT-RESPONSE on the paid answer
                return start_response(status_line, list(hdrs) + extra, exc_info)
            return app(environ, start_with_receipt)
        return wrapped


class _H(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


def _new_close(seller, c, cid, b):
    import chain as ch
    tx = ch.Chain.new_tx(seller, c["buyer"], b["i"] * c["unit"], "channel:close")
    tx.update(kind="channel_close", channel=cid, ticks=b["i"], word=b["w"])
    return tx


def _json(start_response, status, obj, extra=()):
    body = json.dumps(obj).encode()
    reason = {200: "OK", 400: "Bad Request", 402: "Payment Required", 403: "Forbidden", 404: "Not Found"}.get(status, "")
    start_response(f"{status} {reason}", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
                   + list(extra))
    return [body]
