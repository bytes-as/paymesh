"""paymesh mini blockchain: sha256-linked blocks, small PoW, chain.json persistence.

Stdlib only. Run directly to mine a few blocks as a self-test:
    py -3 chain.py
"""
import hashlib
import json
import os
import time

import wallets

DIFFICULTY = 2  # hash must start with this many zeros; tuned so block time < 1s
CHAIN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chain.json")
FAUCET = "0" * 64  # only the faucet may send unsigned txs


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


class Chain:
    def __init__(self, path=CHAIN_FILE):
        self.path = path
        self.blocks = []
        self.pending = []
        if os.path.exists(self.path):
            self.load()

    # --- persistence ---
    def load(self):
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.blocks = [Block(b["index"], b["timestamp"], b["txs"], b["prev_hash"], b["nonce"]) for b in raw]

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([b.to_dict() for b in self.blocks], f, indent=1)

    # --- chain ops ---
    def genesis(self):
        g = Block(0, time.time(), [], "0" * 64)
        self._mine(g)
        self.blocks.append(g)
        self.save()
        return g

    @property
    def tip_hash(self) -> str:
        if not self.blocks:
            self.genesis()
        return self.blocks[-1].hash()

    def new_tx(self, from_wallet: str, to_wallet: str, amount: int, memo: str) -> dict:
        return {"from_wallet": from_wallet, "to_wallet": to_wallet,
                "amount": int(amount), "memo": memo,
                "txid": sha256_hex(f"{from_wallet}{to_wallet}{int(amount)}{memo}{time.time_ns()}{len(self.pending)}")}

    def add_tx(self, tx: dict):
        ok, why = wallets.verify_tx(tx)
        if not ok and not (tx["from_wallet"] == FAUCET and tx["memo"].startswith("faucet")):
            raise ValueError(f"tx rejected: {why}")
        self.pending.append(tx)

    def mine(self) -> Block:
        b = Block(len(self.blocks), time.time(), self.pending, self.tip_hash)
        self.pending = []
        self._mine(b)
        self.blocks.append(b)
        self.save()
        return b

    @staticmethod
    def _mine(b: Block):
        target = "0" * DIFFICULTY
        while not b.hash().startswith(target):
            b.nonce += 1

    # --- validation / ledger ---
    def valid(self) -> tuple[bool, str]:
        prev = "0" * 64
        for b in self.blocks:
            if b.prev_hash != prev:
                return False, f"block {b.index}: broken link"
            if not b.hash().startswith("0" * DIFFICULTY):
                return False, f"block {b.index}: bad PoW"
            for tx in b.txs:
                ok, why = wallets.verify_tx(tx)
                if not ok and not (tx["from_wallet"] == FAUCET and tx["memo"].startswith("faucet")):
                    return False, f"block {b.index}: bad tx {tx['txid'][:8]} ({why})"
            prev = b.hash()
        return True, "ok"

    def balances(self) -> dict:
        out = {}
        for b in self.blocks:
            for tx in b.txs:
                out[tx["from_wallet"]] = out.get(tx["from_wallet"], 0) - tx["amount"]
                out[tx["to_wallet"]] = out.get(tx["to_wallet"], 0) + tx["amount"]
        return out

    def balance(self, wallet: str) -> int:
        return self.balances().get(wallet, 0)

    def all_txs(self) -> list:
        return [tx for b in self.blocks for tx in b.txs]

    def find_tx_by_id(self, txid: str):
        """The tx (if any) with this txid, inside its mined block."""
        for b in self.blocks:
            for tx in b.txs:
                if tx["txid"] == txid:
                    return tx, b
        return None, None


if __name__ == "__main__":
    selftest = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest-chain.json")
    if os.path.exists(selftest):
        os.remove(selftest)
    c = Chain(selftest)
    c.genesis()
    w1 = wallets.load_or_create("chain-selftest-a")
    w2 = wallets.load_or_create("chain-selftest-b")
    fund_a = c.new_tx(FAUCET, w1.address, 10, "faucet:selftest")
    c.add_tx(fund_a)
    tx = c.new_tx(w1.address, w2.address, 10, "test")
    wallets.sign_tx(tx, w1)
    c.add_tx(tx)
    t0 = time.time()
    blk = c.mine()
    dt = time.time() - t0
    assert c.valid()[0]
    assert c.balance(w2.address) == 10
    print(f"self-test ok: mined block in {dt:.2f}s, balance {c.balance(w2.address)}, "
          f"blocks={len(c.blocks)}; signed tx accepted, faucet unsigned ok")
    assert dt < 1.0, "block too slow: lower DIFFICULTY"
