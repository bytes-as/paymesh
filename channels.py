"""paymesh payment channels (PayWord hash chains): pay per call without a tx per call.

On-chain once:  the buyer locks ticks*unit PMX with a signed channel_open tx
                that commits to anchor = sha256^ticks(secret).
Per call:       the buyer sends  X-Channel: <channel id> <i> <word_i>  where
                sha256(word_i) = word_(i-1) and word_0 = anchor. The seller
                checks it with (i - last) hashes, records it, and serves.
On-chain once:  the seller settles with channel_close (its best i and word):
                i*unit to the seller, the rest back to the buyer.

A word can't be forged (that means inverting sha256), a spent word can't be
replayed (the seller only accepts i above the last one it recorded, and keeps
that record on disk), and the buyer's deposit is safe: the seller can only
claim what the buyer revealed, and after `expires` the buyer can refund an
unsettled channel. Stdlib only.
"""
import json
import os
import secrets
import threading

import chain as ch
import wallets


class BuyerChannel:
    """The buyer's side: the hash chain's words and how many ticks are spent."""

    def __init__(self, ticks: int, seed: bytes | None = None):
        self.n = ticks
        self.seed = seed or secrets.token_bytes(32)
        words = [self.seed.hex()]
        for _ in range(ticks):
            words.append(ch.hash_word(words[-1]))
        words.reverse()  # words[0] = anchor, words[i] = word_i
        self.words = words
        self.anchor = words[0]
        self.spent = 0
        self.id = None  # txid of the channel_open, once sent

    def open_tx(self, chain_, wallet, seller: str, unit: int, expires_in: int = 10_000) -> dict:
        """A signed channel_open tx locking ticks*unit PMX for `seller`."""
        tx = chain_.new_tx(wallet.address, seller, self.n * unit, "channel:open")
        tx.update(kind="channel_open", anchor=self.anchor, ticks=self.n, unit=unit,
                  expires=len(chain_.blocks) + expires_in)
        wallets.sign_tx(tx, wallet, chain_)
        self.id = tx["txid"]
        return tx

    def pay(self, ticks: int) -> str:
        """The X-Channel header value paying `ticks` more ticks."""
        if self.id is None:
            raise ValueError("channel not opened")
        if self.spent + ticks > self.n:
            raise ValueError(f"channel exhausted: {self.n - self.spent} ticks left, need {ticks}")
        self.spent += ticks
        return f"{self.id} {self.spent} {self.words[self.spent]}"

    def refund(self, ticks: int):
        """Undo the last `ticks` after a call the seller refused (the word was not accepted)."""
        self.spent -= ticks


class SellerBook:
    """The seller's side: the best (i, word) seen per channel, kept on disk."""

    def __init__(self, path: str, seller_address: str):
        self.path = path
        self.seller = seller_address
        self.lock = threading.Lock()
        self.book: dict = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.book = json.load(f)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.book, f)
        os.replace(tmp, self.path)

    def accept(self, chain_, header: str, price: int) -> tuple[bool, str]:
        """Check an X-Channel header pays `price` PMX; record it if so."""
        parts = (header or "").split()
        if len(parts) != 3 or not parts[1].isdigit():
            return False, "X-Channel must be '<channel id> <i> <word>'"
        cid, i, w = parts[0], int(parts[1]), parts[2].lower()
        if not ch.HEX64.match(w):
            return False, "channel word must be 64 hex"
        with chain_.lock:
            c = chain_.ledger.channels.get(cid)  # mined channels only
            c = dict(c) if c else None
        if c is None:
            return False, "unknown channel (is its channel_open mined?)"
        if not c["open"]:
            return False, "channel already settled"
        if c["seller"] != self.seller:
            return False, "channel pays someone else"
        with self.lock:
            last = self.book.get(cid, {"i": 0, "w": c["anchor"]})
            if not last["i"] < i <= c["n"]:
                return False, f"tick {i} not after the last paid tick {last['i']} (max {c['n']})"
            if (i - last["i"]) * c["unit"] < price:
                return False, f"pays {(i - last['i']) * c['unit']} PMX, price is {price}"
            if ch.hash_word(w, i - last["i"]) != last["w"]:
                return False, "word is not on this channel's hash chain"
            self.book[cid] = {"i": i, "w": w}
            self._save()
        return True, f"paid {(i - last['i']) * c['unit']} PMX on channel {cid[:12]} (tick {i}/{c['n']})"

    def best(self, cid: str):
        with self.lock:
            return self.book.get(cid)

    def close_tx(self, chain_, seller_wallet, cid: str) -> dict:
        """A signed channel_close claiming the best word seen."""
        c = chain_.ledger.channels.get(cid)
        b = self.best(cid)
        if c is None or not c["open"]:
            raise ValueError("no open channel with that id")
        if not b:
            raise ValueError("nothing paid on this channel yet; the buyer can refund it after expiry")
        tx = chain_.new_tx(self.seller, c["buyer"], b["i"] * c["unit"], "channel:close")
        tx.update(kind="channel_close", channel=cid, ticks=b["i"], word=b["w"])
        wallets.sign_tx(tx, seller_wallet, chain_)
        return tx

    def expiring(self, chain_, within: int) -> list:
        """Open channels this seller was paid on that expire within `within` blocks."""
        h = len(chain_.blocks)
        with chain_.lock:
            return [cid for cid, c in chain_.ledger.channels.items()
                    if c["open"] and c["seller"] == self.seller and cid in self.book and c["expires"] - h <= within]


def refund_tx(chain_, wallet, cid: str) -> dict:
    """The buyer's channel_refund after expiry, if the seller never settled."""
    c = chain_.ledger.channels.get(cid)
    if c is None:
        raise ValueError("unknown channel")
    tx = chain_.new_tx(wallet.address, wallet.address, c["n"] * c["unit"], "channel:refund")
    tx.update(kind="channel_refund", channel=cid)
    wallets.sign_tx(tx, wallet, chain_)
    return tx
