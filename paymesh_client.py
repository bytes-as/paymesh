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


class PaymentError(Exception):
    pass


class Client:
    def __init__(self, api: str, wallet: str, timeout: float = 10.0):
        self.api = api.rstrip("/")
        self.base_path = urllib.parse.urlsplit(self.api).path
        self._tl = threading.local()
        self.wallet = wallets.load_or_create(wallet)
        self.timeout = timeout
        self.prices: dict = {}  # path -> PMX, learned from 402s
        self.channel: channels.BuyerChannel | None = None
        self.terms: dict | None = None
        self._lock = threading.Lock()
        self._load_channel()

    # --- HTTP ---
    def _conn(self):
        """This thread's kept-alive connection. Connect blocking, then set the timeout:
        on Windows a timed connect costs ~15 ms per request."""
        c = getattr(self._tl, "conn", None)
        if c is None:
            u = urllib.parse.urlsplit(self.api)
            c = http.client.HTTPConnection(u.hostname, u.port or 80)
            c.sock = socket.create_connection((u.hostname, u.port or 80))
            c.sock.settimeout(self.timeout)
            c.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._tl.conn, self._tl.fresh = c, True
        return c

    def _drop(self):
        c = getattr(self._tl, "conn", None)
        if c is not None:
            c.close()
        self._tl.conn = None

    def _req(self, path, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else None
        h = {"Content-Type": "application/json"} if body else {}
        h.update(headers or {})
        for _ in range(2):
            c = self._conn()
            reused, self._tl.fresh = not self._tl.fresh, False
            try:
                c.request("POST" if body is not None else "GET", self.base_path + path, body=body, headers=h)
                r = c.getresponse()
                raw = r.read().decode()
                if r.will_close:
                    self._drop()
                break
            except (http.client.HTTPException, OSError):
                self._drop()
                if not reused:  # a fresh connection failed: the server is down
                    raise
                # the server closed an idle kept-alive connection: retry once on a new one
        try:
            return r.status, json.loads(raw or "null")
        except json.JSONDecodeError:
            return r.status, {"error": raw}

    def _ledger(self) -> "_Remote":
        """The server's view of this wallet: balance, current key, chain height."""
        code, info = self._req(f"/account?address={self.wallet.address}")
        if code != 200:
            raise PaymentError(f"account: {info}")
        return _Remote(info)

    def balance(self) -> int:
        return self._ledger().balance

    def faucet(self) -> dict:
        code, body = self._req(f"/faucet?address={self.wallet.address}")
        if code != 200:
            raise PaymentError(f"faucet: {body}")
        return body

    def _submit(self, tx) -> dict:
        code, body = self._req("/tx", tx)
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
        self.prices[path] = price = body["amount"]
        if self.channel:
            return self._via_channel(path, price)
        return self._on_chain(path, body)

    def _via_channel(self, path, price):
        ticks = math.ceil(price / self.terms["unit"])
        with self._lock:
            try:
                header = self.channel.pay(ticks)
            except ValueError as e:  # the channel ran out
                raise PaymentError(str(e)) from None
            self._save_channel()
        code, body = self._req(path, headers={"X-Channel": header})
        if code == 402:
            raise PaymentError(f"channel payment refused: {body.get('reason')}")
        return code, body

    def _on_chain(self, path, challenge):
        led = self._ledger()
        if led.balance < challenge["amount"]:
            raise PaymentError(f"insufficient funds for {challenge['amount']} PMX")
        tx = led.new_tx(self.wallet.address, challenge["recipient"], challenge["amount"], challenge["memo"])
        wallets.sign_tx(tx, self.wallet, led)
        self._submit(tx)
        return self._req(path, headers={"X-Payment": tx["txid"]})


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
