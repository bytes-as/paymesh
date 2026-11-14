"""paymesh mini blockchain: sha256-linked blocks, small PoW, chain.json persistence.

Every tx goes through one rule set, `Ledger.apply`, whether it arrives over
the API, sits in a block being mined, or is read back from chain.json:

* shape: 64-hex addresses and txid, a whole positive amount, a short memo;
* a txid is used once (a signed tx cannot be resubmitted to charge twice);
* the faucet (zero address) mints with an unsigned "faucet:" tx;
* any other sender must sign with its CURRENT one-time key (wallets.py) and
  hold the amount, counting txs already pending.

Loading chain.json replays every block through the same rules, so a file
edited by hand (a changed amount, a broken link) is refused.

Stdlib only. Run directly for a self-test:
    py -3 chain.py
"""
import copy
import hashlib
import json
import os
import re
import threading
import time

import wallets

DIFFICULTY = 2  # hash must start with this many zeros; tuned so block time < 1s
CHAIN_FILE = os.path.join(wallets.HOME, "chain.json")
FAUCET = "0" * 64  # only the faucet may send unsigned txs
MAX_MEMO = 256
FAUCET_MAX = 100  # PMX per faucet mint
HEX64 = re.compile(r"^[0-9a-f]{64}$")
FORMAT = 3  # chain.json: a {"format": 3} line, then one block per line (append-only)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class Block:
    def __init__(self, index, timestamp, txs, prev_hash, nonce=0):
        self.index = index
        self.timestamp = timestamp
        self.txs = txs
        self.prev_hash = prev_hash
        self.nonce = nonce

    def header(self) -> str:
        return f"{self.index}|{self.timestamp}|{json.dumps(self.txs, sort_keys=True)}|{self.prev_hash}|{self.nonce}"

    def hash(self) -> str:
        return sha256_hex(self.header())

    def to_dict(self):
        return {"index": self.index, "timestamp": self.timestamp, "txs": self.txs,
                "prev_hash": self.prev_hash, "nonce": self.nonce, "hash": self.hash()}


MAX_TICKS = 1_000_000  # longest hash chain a channel may commit to


def hash_word(w: str, times: int = 1) -> str:
    """Apply sha256 to a 64-hex word `times` times (PayWord hash chain step)."""
    b = bytes.fromhex(w)
    for _ in range(times):
        b = hashlib.sha256(b).digest()
    return b.hex()


class Ledger:
    """Balances, each address's current one-time key, every txid seen, and payment channels.

    Payment channels (PayWord): the buyer locks n*unit PMX with a channel_open
    tx that commits to anchor = sha256^n(secret). Each paid call reveals the
    next word of the chain off-chain; the seller checks it with a few hashes.
    The seller settles once with channel_close (ticks i and word w_i with
    sha256^i(w_i) == anchor): i*unit to the seller, the rest back to the buyer.
    If the seller never closes, the buyer takes the deposit back with
    channel_refund once the channel's expiry height has passed.
    """

    def __init__(self):
        self.balances: dict = {}
        self.keys: dict = {}
        self.txids: set = set()
        self.channels: dict = {}  # open txid -> {buyer, seller, anchor, n, unit, expires, open}
        self.height = 0  # blocks applied; pending txs are judged at this height

    def current_key(self, address: str) -> str:
        return self.keys.get(address, address)  # key 0's fingerprint is the address

    def _signed_by(self, tx, who: str):
        ok, why = wallets.verify_tx(tx, self.current_key(who))
        if not ok:
            raise ValueError(why)

    def _check_channel(self, tx, kind):
        frm, to, amt = tx["from_wallet"], tx["to_wallet"], tx["amount"]
        if frm == FAUCET:
            raise ValueError("the faucet cannot use channels")
        if kind == "channel_open":
            anchor, n, unit, exp = (tx.get(k) for k in ("anchor", "ticks", "unit", "expires"))
            if not isinstance(anchor, str) or not HEX64.match(anchor):
                raise ValueError("channel anchor must be 64 hex")
            for name, v, lo, hi in (("ticks", n, 1, MAX_TICKS), ("unit", unit, 1, 10**9), ("expires", exp, self.height + 1, self.height + 100_000)):
                if type(v) is not int or not lo <= v <= hi:
                    raise ValueError(f"channel {name} must be a whole number in {lo}..{hi}")
            if amt != n * unit:
                raise ValueError("channel deposit (amount) must equal ticks * unit")
            if frm == to:
                raise ValueError("a channel pays someone else")
            self._signed_by(tx, frm)
            if self.balances.get(frm, 0) < amt:
                raise ValueError(f"insufficient funds: {self.balances.get(frm, 0)} PMX, needs {amt}")
            return
        ch = self.channels.get(tx.get("channel"))
        if ch is None:
            raise ValueError("unknown channel")
        if not ch["open"]:
            raise ValueError("channel already settled")
        if kind == "channel_close":
            i, w = tx.get("ticks"), tx.get("word")
            if frm != ch["seller"] or to != ch["buyer"]:
                raise ValueError("channel_close goes from the seller to the buyer")
            if type(i) is not int or not 1 <= i <= ch["n"] or not isinstance(w, str) or not HEX64.match(w):
                raise ValueError("channel_close needs ticks in 1..n and a 64-hex word")
            if hash_word(w, i) != ch["anchor"]:
                raise ValueError("word does not hash to the channel anchor in that many ticks")
            if amt != i * ch["unit"]:
                raise ValueError("channel_close amount must equal ticks * unit")
            self._signed_by(tx, frm)
            return
        if kind == "channel_refund":
            if frm != ch["buyer"] or to != ch["buyer"]:
                raise ValueError("only the buyer refunds a channel, to itself")
            if self.height < ch["expires"]:
                raise ValueError(f"channel refundable from block {ch['expires']}")
            if amt != ch["n"] * ch["unit"]:
                raise ValueError("refund amount must equal the deposit")
            self._signed_by(tx, frm)
            return
        raise ValueError(f"unknown tx kind {kind!r}")

    def check(self, tx) -> None:
        """Raise ValueError if tx breaks a rule; change nothing."""
        if not isinstance(tx, dict):
            raise ValueError("tx must be an object")
        frm, to, amt, memo, txid = (tx.get(k) for k in ("from_wallet", "to_wallet", "amount", "memo", "txid"))
        for name, v in (("from_wallet", frm), ("to_wallet", to), ("txid", txid)):
            if not isinstance(v, str) or not HEX64.match(v):
                raise ValueError(f"{name} must be 64 lowercase hex characters")
        if type(amt) is not int or amt <= 0:
            raise ValueError("amount must be a whole number of PMX above 0")
        if not isinstance(memo, str) or len(memo) > MAX_MEMO:
            raise ValueError(f"memo must be text of at most {MAX_MEMO} characters")
        if txid in self.txids:
            raise ValueError("txid already used (a tx counts once)")
        if to == FAUCET:
            raise ValueError("cannot pay the faucet address")
        kind = tx.get("kind", "pay")
        if kind != "pay":
            return self._check_channel(tx, kind)
        if frm == FAUCET:
            if not memo.startswith("faucet:"):
                raise ValueError("faucet txs carry a faucet: memo")
            if amt > FAUCET_MAX:
                raise ValueError(f"a faucet mint is at most {FAUCET_MAX} PMX")
            return
        ok, why = wallets.verify_tx(tx, self.current_key(frm))
        if not ok:
            raise ValueError(why)
        have = self.balances.get(frm, 0)
        if have < amt:
            raise ValueError(f"insufficient funds: {have} PMX, needs {amt}")

    def apply(self, tx) -> None:
        self.check(tx)
        frm, to, amt = tx["from_wallet"], tx["to_wallet"], tx["amount"]
        kind = tx.get("kind", "pay")
        if frm != FAUCET:
            self.keys[frm] = tx["next_key"]
        if kind == "pay":
            if frm != FAUCET:
                self.balances[frm] = self.balances.get(frm, 0) - amt
            self.balances[to] = self.balances.get(to, 0) + amt
        elif kind == "channel_open":  # deposit leaves the buyer, held by the channel
            self.balances[frm] = self.balances.get(frm, 0) - amt
            self.channels[tx["txid"]] = {"buyer": frm, "seller": to, "anchor": tx["anchor"], "n": tx["ticks"],
                                         "unit": tx["unit"], "expires": tx["expires"], "open": True}
        elif kind == "channel_close":
            ch = self.channels[tx["channel"]]
            deposit = ch["n"] * ch["unit"]
            self.balances[ch["seller"]] = self.balances.get(ch["seller"], 0) + amt
            self.balances[ch["buyer"]] = self.balances.get(ch["buyer"], 0) + deposit - amt
            ch.update(open=False, paid=amt)
        elif kind == "channel_refund":
            ch = self.channels[tx["channel"]]
            self.balances[frm] = self.balances.get(frm, 0) + amt
            ch.update(open=False, paid=0)
        self.txids.add(tx["txid"])


class Chain:
    def __init__(self, path=CHAIN_FILE):
        self.path = path
        self.blocks = []
        self.pending = []
        self.ledger = Ledger()  # mined blocks only
        self.pledger = Ledger()  # mined blocks + pending txs
        self.lock = threading.RLock()
        if os.path.exists(self.path):
            self.load()

    # --- persistence ---
    def load(self):
        with open(self.path, "r", encoding="utf-8") as f:
            text = f.read()
        upgrade = False
        try:  # format 1 (a list) or 2 (one object): a single JSON document
            raw = json.loads(text)
        except json.JSONDecodeError:
            raw = None
        if isinstance(raw, list):
            raise ValueError(f"{self.path}: format 1 chain (before key rotation); archive it and start a new chain")
        if isinstance(raw, dict) and raw.get("format") == 2:
            dicts, upgrade = raw["blocks"], True
        else:
            lines = text.split("\n")
            head = json.loads(lines[0]) if lines and lines[0].strip() else {}
            if head.get("format") != FORMAT:
                raise ValueError(f"{self.path}: unknown chain format {head.get('format')}")
            dicts, good = [], len(lines[0]) + 1
            for n, line in enumerate(lines[1:], 1):
                if not line.strip():
                    continue
                try:
                    dicts.append(json.loads(line))
                except json.JSONDecodeError:
                    if n == len(lines) - 1:  # a block cut off by a crash mid-write: drop it
                        print(f"[chain] dropping a partly written last block in {self.path}")
                        with open(self.path, "r+b") as f:  # cut at the last newline, in bytes
                            data = f.read()
                            f.truncate(data.rstrip(b"\r\n").rfind(b"\n") + 1)
                        break
                    raise ValueError(f"{self.path}: line {n + 1} is not a block")
                good += len(line) + 1
        blocks = [Block(b["index"], b["timestamp"], b["txs"], b["prev_hash"], b["nonce"]) for b in dicts]
        ledger, why = self._replay(blocks)
        if ledger is None:
            raise ValueError(f"{self.path}: invalid chain: {why}")
        self.blocks, self.ledger, self.pledger, self.pending = blocks, ledger, copy.deepcopy(ledger), []
        if upgrade:
            self.save()  # rewrite once as format 3

    def save(self):
        """Write the whole chain (genesis, upgrades). Mining appends instead."""
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"format": FORMAT}) + "\n")
            for b in self.blocks:
                f.write(json.dumps(b.to_dict()) + "\n")
        os.replace(tmp, self.path)

    def _append(self, b: Block):
        """Add one block to the file: cost stays flat as the chain grows."""
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(b.to_dict()) + "\n")

    @classmethod
    def open_or_archive(cls, path=CHAIN_FILE):
        """Open the chain; an old-format chain is renamed aside (never deleted) and a new one started."""
        try:
            c = cls(path)
        except ValueError as e:
            if "format 1" not in str(e):
                raise
            stamp = time.strftime("%Y%m%d-%H%M%S")
            aside = f"{os.path.splitext(path)[0]}.v1-{stamp}.json"
            os.replace(path, aside)
            print(f"[chain] {e}\n[chain] old chain kept as {aside}; starting a new chain")
            c = cls(path)
        if not c.blocks:
            c.genesis()
        return c

    # --- chain ops ---
    def genesis(self):
        with self.lock:
            g = Block(0, time.time(), [], "0" * 64)
            self._mine(g)
            self.blocks.append(g)
            self.ledger.height = self.pledger.height = 1
            self.save()
            return g

    @property
    def tip_hash(self) -> str:
        if not self.blocks:
            self.genesis()
        return self.blocks[-1].hash()

    @property
    def height(self) -> int:
        return len(self.blocks)

    @staticmethod
    def new_tx(from_wallet: str, to_wallet: str, amount: int, memo: str) -> dict:
        return {"from_wallet": from_wallet, "to_wallet": to_wallet,
                "amount": amount, "memo": memo,
                "txid": sha256_hex(f"{from_wallet}{to_wallet}{amount}{memo}{time.time_ns()}{os.urandom(8).hex()}")}

    def current_key(self, address: str) -> str:
        """The key fingerprint that must sign this address's next tx (pending txs included)."""
        with self.lock:
            return self.pledger.current_key(address)

    def add_tx(self, tx: dict):
        with self.lock:
            self.pledger.apply(tx)  # raises ValueError on any broken rule
            self.pending.append(tx)

    def mine(self) -> Block:
        with self.lock:
            b = Block(len(self.blocks), time.time(), self.pending, self.tip_hash)
            self._mine(b)
            self.pending = []
            self.blocks.append(b)
            self.ledger.height = b.index
            for tx in b.txs:
                self.ledger.apply(tx)
            self.ledger.height = self.pledger.height = len(self.blocks)
            self._append(b)
            return b

    @staticmethod
    def _mine(b: Block):
        target = "0" * DIFFICULTY
        while not b.hash().startswith(target):
            b.nonce += 1

    # --- validation / ledger ---
    @staticmethod
    def _replay(blocks):
        ledger = Ledger()
        prev = "0" * 64
        for n, b in enumerate(blocks):
            if b.index != n:
                return None, f"block {n}: wrong index {b.index}"
            if b.prev_hash != prev:
                return None, f"block {b.index}: broken link"
            if not b.hash().startswith("0" * DIFFICULTY):
                return None, f"block {b.index}: bad PoW"
            ledger.height = n
            for tx in b.txs:
                try:
                    ledger.apply(tx)
                except ValueError as e:
                    return None, f"block {b.index}: bad tx {str(tx.get('txid', '?'))[:8]} ({e})"
            ledger.height = n + 1
            prev = b.hash()
        return ledger, "ok"

    def valid(self) -> tuple[bool, str]:
        with self.lock:
            ledger, why = self._replay(self.blocks)
            return ledger is not None, why

    def balances(self) -> dict:
        with self.lock:
            return {a: v for a, v in self.ledger.balances.items() if a != FAUCET}

    def balance(self, wallet: str) -> int:
        with self.lock:
            return self.ledger.balances.get(wallet, 0)

    def all_txs(self) -> list:
        with self.lock:
            return [tx for b in self.blocks for tx in b.txs]

    def find_tx_by_id(self, txid: str):
        """The tx (if any) with this txid, inside its mined block."""
        with self.lock:
            for b in reversed(self.blocks):
                for tx in b.txs:
                    if tx["txid"] == txid:
                        return tx, b
        return None, None


if __name__ == "__main__":
    import tempfile

    tmp = tempfile.mkdtemp()
    wallets.WALLET_DIR = os.path.join(tmp, "wallets")
    c = Chain(os.path.join(tmp, "chain.json"))
    c.genesis()
    w1 = wallets.fund(c, "chain-selftest-a", 10)
    w2 = wallets.fund(c, "chain-selftest-b", 0)
    tx = c.new_tx(w1.address, w2.address, 10, "test")
    wallets.sign_tx(tx, w1, c)
    c.add_tx(tx)
    t0 = time.time()
    c.mine()
    dt = time.time() - t0
    assert c.valid()[0]
    assert c.balance(w2.address) == 10
    reopened = Chain(c.path)
    assert reopened.balance(w2.address) == 10
    print(f"self-test ok: mined block in {dt:.2f}s, balance {c.balance(w2.address)}, "
          f"blocks={len(c.blocks)}; chain reloads and replays clean")
    assert dt < 1.0, "block too slow: lower DIFFICULTY"
