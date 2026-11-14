"""paymesh client SDK: call paid APIs from an agent in three lines.

    from paymesh_client import Client
    api = Client("http://127.0.0.1:8402", wallet="my-agent")
    api.open_channel(ticks=500)                  # one on-chain deposit (optional)
    status, data = api.get("/premium/price")     # pays by itself

Without a channel each paid call is an on-chain tx (402 -> signed tx -> retry).
With a channel each call is a single request carrying the next hash-chain word,
and the price of each route is learned from its first 402. The channel's state
(secret seed, ticks spent) is kept beside the wallet, so an agent can stop and
resume. Stdlib only.
"""
import http.client
import json
import math
import os
import socket
import threading
import urllib.parse

import chain as ch
import channels
import wallets
import paymesh_x402 as x402
class PaymentError(Exception):
    pass


class Client:
    SAVE_EVERY = 64  # calls between saves of the channel state (resynced from the seller on start)
    def __init__(self, api: str, wallet: str, timeout: float = 10.0, node: str | None = None):
        """api: the paid service. node: the paymesh node (txs, balances, faucet);
        the same server unless the seller runs its API apart from the node."""
        self.api = api.rstrip("/")
        self.node = (node or api).rstrip("/")
        self._tl = threading.local()
        self.wallet = wallets.load_or_create(wallet)
        self.timeout = timeout
        self.prices: dict = {}  # path -> PMX, learned from 402s
        self.offers: dict = {}  # path -> {method: x402 PaymentRequirements} from PAYMENT-REQUIRED
        self.channel: channels.BuyerChannel | None = None
        self.terms: dict | None = None
        self._lock = threading.Lock()
        self._calls = 0
        self._load_channel()

    # --- HTTP ---
    def _conn(self, base):
        """This thread's kept-alive connection to `base`. Connect blocking, then set the
        timeout: on Windows a timed connect costs ~15 ms per request."""
        conns = self._tl.__dict__.setdefault("conns", {})
        c = conns.get(base)
        if c is None:
            u = urllib.parse.urlsplit(base)
            c = http.client.HTTPConnection(u.hostname, u.port or 80)
            c.sock = socket.create_connection((u.hostname, u.port or 80))
            c.sock.settimeout(self.timeout)
            c.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conns[base] = c
            self._tl.fresh = True
        return c

    def _drop(self, base):
        c = self._tl.__dict__.get("conns", {}).pop(base, None)
        if c is not None:
            c.close()

    def _node(self, path, data=None, headers=None):
        return self._req(path, data, headers, base=self.node)

    def _req(self, path, data=None, headers=None, base=None):
        base = base or self.api
        body = json.dumps(data).encode() if data is not None else None
        h = {"Content-Type": "application/json"} if body else {}
        h.update(headers or {})
        for _ in range(2):
            self._tl.fresh = False
            c = self._conn(base)
            reused = not self._tl.fresh
            try:
                c.request("POST" if body is not None else "GET", urllib.parse.urlsplit(base).path + path, body=body, headers=h)
                r = c.getresponse()
                raw = r.read().decode()
                self._tl.headers = r.headers  # x402 headers of the last answer on this thread
                if r.will_close:
                    self._drop(base)
                break
            except (http.client.HTTPException, OSError):
                self._drop(base)
                if not reused:  # a fresh connection failed: the server is down
                    raise
                # the server closed an idle kept-alive connection: retry once on a new one
        try:
            return r.status, json.loads(raw or "null")
        except json.JSONDecodeError:
            return r.status, {"error": raw}

    def _ledger(self) -> "_Remote":
        """The server's view of this wallet: balance, current key, chain height."""
        code, info = self._node(f"/account?address={self.wallet.address}")
        if code != 200:
            raise PaymentError(f"account: {info}")
        return _Remote(info)

    def balance(self) -> int:
        return self._ledger().balance

    def faucet(self) -> dict:
        code, body = self._node(f"/faucet?address={self.wallet.address}")
        if code != 200:
            raise PaymentError(f"faucet: {body}")
        return body

    def _submit(self, tx) -> dict:
        code, body = self._node("/tx", tx)
        if code != 200:
            raise PaymentError(f"tx refused: {body.get('error') if isinstance(body, dict) else body}")
        return body

    # --- channels ---
    def _channel_file(self):
        return os.path.join(wallets.WALLET_DIR, "channels", f"{self.wallet.name}.json")

    def _save_channel(self):
        c = self.channel
        os.makedirs(os.path.dirname(self._channel_file()), exist_ok=True)
        tmp = self._channel_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"id": c.id, "seed": c.seed.hex(), "ticks": c.n, "spent": c.spent, "terms": self.terms}, f)
        os.replace(tmp, self._channel_file())

    def _load_channel(self):
        p = self._channel_file()
        if not os.path.exists(p):
            return
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        try:
            open_ids = self._ledger().channels
        except (PaymentError, OSError):
            return  # server down: decide on the next start
        if raw["id"] not in open_ids:
            os.remove(p)  # settled or refunded: start fresh next time
            return
        c = channels.BuyerChannel(raw["ticks"], bytes.fromhex(raw["seed"]))
        c.id, c.spent = raw["id"], raw["spent"]
        # the file is saved every SAVE_EVERY calls: the seller knows the last tick it took
        code, st = self._req(f"/channel/state?channel={c.id}")
        if code == 200:
            c.spent = max(c.spent, st["paid_ticks"])
        self.channel, self.terms = c, raw["terms"]

    def open_channel(self, ticks: int, expires_in: int = 10_000) -> str:
        """Lock ticks*unit PMX for the API's seller; returns the channel id."""
        if self.channel:
            raise PaymentError("a channel is already open; close it first")
        code, terms = self._req("/channel/terms")
        if code != 200:
            raise PaymentError(f"no channel terms: {terms}")
        led = self._ledger()
        c = channels.BuyerChannel(ticks)
        tx = c.open_tx(led, self.wallet, terms["seller"], terms["unit"], expires_in)
        self._submit(tx)
        self.channel, self.terms = c, terms
        self._save_channel()
        return c.id

    def close_channel(self) -> dict:
        """Ask the seller to settle; the unspent deposit comes back."""
        if not self.channel:
            raise PaymentError("no open channel")
        code, body = self._req("/channel/close", {"channel": self.channel.id})
        if code != 200:
            raise PaymentError(f"close refused: {body}")
        self.channel = None
        os.remove(self._channel_file())
        return body

    # --- paid calls ---
    def get(self, path: str):
        """GET a route, paying for it if it asks. Returns (status, json)."""
        price = self.prices.get(path)
        if self.channel and price is not None:
            return self._via_channel(path, price)
        code, body = self._req(path)
        if code != 402:
            return code, body
        offers = self._offers(path)
        self.prices[path] = price = int(offers["tx"]["amount"])
        if self.channel:
            return self._via_channel(path, price)
        return self._on_chain(path, offers["tx"])

    def _offers(self, path) -> dict:
        """The paymesh entries of this 402's PAYMENT-REQUIRED header, by transfer method."""
        try:
            req = x402.decode(self._tl.headers.get(x402.REQUIRED) or "")
            offers = {a["extra"]["assetTransferMethod"]: a for a in req.get("accepts", [])
                      if isinstance(a, dict) and a.get("scheme") == x402.SCHEME and isinstance(a.get("extra"), dict)}
        except (x402.Invalid, KeyError, TypeError) as e:
            raise PaymentError(f"402 without a usable {x402.REQUIRED} header: {e}") from None
        if "tx" not in offers:
            raise PaymentError(f"402 offers no {x402.SCHEME!r} payment")
        self.offers[path] = offers
        return offers

    def last_receipt(self) -> dict | None:
        """The decoded PAYMENT-RESPONSE (x402 SettlementResponse) of this thread's last call."""
        h = getattr(self._tl, "headers", None)
        v = h.get(x402.RESPONSE) if h is not None else None
        try:
            return x402.decode(v) if v else None
        except x402.Invalid:
            return None

    def _pay(self, path, accepted, payload):
        sig = x402.encode({"x402Version": x402.VERSION, "accepted": accepted, "payload": payload})
        return self._req(path, headers={x402.SIGNATURE: sig})

    def _refusal(self, body) -> str:
        r = self.last_receipt() or {}
        return r.get("errorMessage") or r.get("errorReason") or (body or {}).get("reason") or str(body)

    def _via_channel(self, path, price):
        ticks = math.ceil(price / self.terms["unit"])
        accepted = self.offers.get(path, {}).get("channel")
        with self._lock:
            try:
                header = self.channel.pay(ticks)
            except ValueError as e:  # the channel ran out
                raise PaymentError(str(e)) from None
            self._calls += 1
            if self._calls % self.SAVE_EVERY == 0:
                self._save_channel()
        cid, i, w = header.split()
        if accepted is None:  # the seller offered no x402 channel entry: legacy header
            code, body = self._req(path, headers={"X-Channel": header})
        else:
            code, body = self._pay(path, accepted, {"channel": cid, "tick": int(i), "word": w})
        if code in (400, 402):
            raise PaymentError(f"channel payment refused: {self._refusal(body)}")
        return code, body

    HEADER_LIMIT = 32_000  # a bigger signed tx (old Lamport key) is mined first and sent by txid

    def _on_chain(self, path, accepted):
        led = self._ledger()
        amount = int(accepted["amount"])
        if led.balance < amount:
            raise PaymentError(f"insufficient funds for {amount} PMX")
        tx = led.new_tx(self.wallet.address, accepted["payTo"], amount, accepted["extra"]["memo"])
        wallets.sign_tx(tx, self.wallet, led)
        payload = {"transaction": tx}  # the seller settles it: one request instead of two
        if len(x402.encode(payload)) > self.HEADER_LIMIT:
            self._submit(tx)
            payload = {"txid": tx["txid"]}
        code, body = self._pay(path, accepted, payload)
        if code in (400, 402):
            why = self._refusal(body)
            if "transaction" in payload:
                self._retire_key(tx)
            raise PaymentError(f"payment refused: {why}")
        return code, body

    def _retire_key(self, exposed: dict):
        """The seller saw `exposed`, signed by a one-time key, and refused it. Before that key
        signs anything else (a retry would), move the ledger past it with a 1 PMX payment to
        ourselves, sent to the node: the seller never holds two signatures from one key."""
        led = self._ledger()
        if led.current_key(self.wallet.address) == exposed["next_key"]:
            return  # the exposed tx got mined after all: the key already rotated
        if led.balance < 1:
            raise PaymentError("payment refused and the signing key could not be retired (balance 0): "
                               "fund the wallet before paying again")
        tx = led.new_tx(self.wallet.address, self.wallet.address, 1, "retire-key")
        wallets.sign_tx(tx, self.wallet, led)
        try:
            self._submit(tx)
        except PaymentError:
            if self._ledger().current_key(self.wallet.address) != exposed["next_key"]:
                raise  # the seller mined the exposed tx meanwhile, or this failed: only the first is fine


class _Remote:
    """Enough of a ledger to sign with: the server says which key signs next."""

    def __init__(self, info: dict):
        self.balance = info["balance"]
        self._key = info["current_key"]
        self.height = info["height"]
        self.channels = info.get("channels", {})

    def current_key(self, address: str) -> str:
        return self._key

    new_tx = staticmethod(ch.Chain.new_tx)
