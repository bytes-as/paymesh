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
import durable
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
                  expires=chain_.height + expires_in)
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
    """The seller's side: the best (i, word) seen per channel, kept on disk.

    Each accepted payment appends one line "<channel> <i> <word>" before the
    call is served, so a restarted seller never takes a spent word again.
    The log is compacted to one line per channel when it grows.

    Hub mode: the book of a hub (the node) takes words of channels opened to
    the hub on behalf of any seller, with accept(..., payee=seller). Each such
    line also names the payee and the PMX it earned ("<channel> <i> <word>
    <payee> <amount>"), one atomic line; the hub's channel_close then pays
    every payee its share (`splits`). One counter per channel, so a word can
    only ever pay one seller. A seller's own book forwards words of a hub's
    channels to the hub: set `hub` to claim(channel_seller, header, price) ->
    (ok, reason), or None when channel_seller is no hub it knows.
    """

    COMPACT_AT = 50_000  # lines
    STOP_WITHIN = 20  # blocks: no ticks once a channel is this close to its refund height

    def __init__(self, path: str, seller_address: str, stop_within: int | None = None):
        self.path = path
        self.stop_within = self.STOP_WITHIN if stop_within is None else stop_within
        self.seller = seller_address
        self.lock = threading.Lock()
        self.book: dict = {}
        self.credits: dict = {}  # hub mode: channel -> {payee: PMX}
        self.hub = None
        self._lines = 0
        cut = durable.repair_tail(path, lambda raw: _record_ok(raw.decode("utf-8", "replace")))
        if cut:
            print(f"[channels] dropped {cut} bytes of a partly written last record in {path}")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not _record_ok(line):
                        continue  # never take a damaged record as the best word
                    parts = line.split()
                    cid, i, w = parts[0], int(parts[1]), parts[2]
                    if i > self.book.get(cid, {"i": 0})["i"]:
                        self.book[cid] = {"i": i, "w": w}
                    if len(parts) == 5:  # every credit line counts (compaction writes totals)
                        cr = self.credits.setdefault(cid, {})
                        cr[parts[3]] = cr.get(parts[3], 0) + int(parts[4])
                    self._lines += 1
        self.log = durable.AppendLog(path)  # fsync policy: durable.py

    def _record(self, cid: str, i: int, w: str, payee: str | None = None, amount: int = 0):
        self.log.append(f"{cid} {i} {w}" + (f" {payee} {amount}" if payee else ""))
        self._lines += 1
        if self._lines > self.COMPACT_AT:
            out = []
            for c, v in self.book.items():
                cr = self.credits.get(c)
                if cr:
                    out += [f"{c} {v['i']} {v['w']} {who} {amt}\n" for who, amt in cr.items()]
                else:
                    out.append(f"{c} {v['i']} {v['w']}\n")
            data = "".join(out).encode()
            self.log.reopen(lambda: durable.atomic_write(self.path, data, sync=durable.POLICY != "off"))
            self._lines = len(out)

    def close(self):
        self.log.close()

    def accept(self, chain_, header: str, price: int, payee: str | None = None) -> tuple[bool, str]:
        """Check an X-Channel header pays `price` PMX; record it if so. payee: hub mode, the
        seller this word pays (the hub credits it)."""
        parts = (header or "").split()
        # isdigit() alone takes '²' (latin-1 in a header), which int() refuses: ASCII digits only
        if len(parts) != 3 or not (parts[1].isascii() and parts[1].isdigit()):
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
            if payee is None and self.hub is not None:
                res = self.hub(c["seller"], f"{cid} {i} {w}", price)
                if res is not None:  # a hub's channel: the hub takes the word for us
                    return res
            return False, "channel pays someone else"
        if payee is not None and (not isinstance(payee, str) or not ch.HEX64.match(payee) or payee == ch.FAUCET):
            return False, "payee must be a 64-hex address"
        height = getattr(chain_, "height", None)
        if height is not None and c["expires"] - height <= self.stop_within:
            # the buyer may refund from `expires` on: a tick taken now might never be settled
            return False, f"channel is refundable at block {c['expires']}, {c['expires'] - height} blocks away: too close to settle"
        with self.lock:
            last = self.book.get(cid, {"i": 0, "w": c["anchor"]})
            if not last["i"] < i <= c["n"]:
                return False, f"tick {i} not after the last paid tick {last['i']} (max {c['n']})"
            paid = (i - last["i"]) * c["unit"]
            if paid < price:
                return False, f"pays {paid} PMX, price is {price}"
            if ch.hash_word(w, i - last["i"]) != last["w"]:
                return False, "word is not on this channel's hash chain"
            if payee is None and self.credits.get(cid):
                return False, "a hub channel pays only through the hub"
            self.book[cid] = {"i": i, "w": w}
            if payee is not None:
                cr = self.credits.setdefault(cid, {})
                cr[payee] = cr.get(payee, 0) + paid
            self._record(cid, i, w, payee, paid)
        return True, f"paid {paid} PMX on channel {cid[:12]} (tick {i}/{c['n']})"

    def best(self, cid: str):
        with self.lock:
            return self.book.get(cid)

    def close_tx(self, chain_, seller_wallet, cid: str) -> dict:
        """A signed channel_close claiming the best word seen; in hub mode it pays every
        seller its share (splits)."""
        c = chain_.ledger.channels.get(cid)
        b = self.best(cid)
        if c is None or not c["open"]:
            raise ValueError("no open channel with that id")
        if not b:
            raise ValueError("nothing paid on this channel yet; the buyer can refund it after expiry")
        tx = chain_.new_tx(self.seller, c["buyer"], b["i"] * c["unit"], "channel:close")
        tx.update(kind="channel_close", channel=cid, ticks=b["i"], word=b["w"])
        with self.lock:
            cr = dict(self.credits.get(cid) or {})
        if cr:
            if sum(cr.values()) != tx["amount"]:
                raise ValueError(f"hub credits {sum(cr.values())} PMX differ from the {tx['amount']} PMX revealed")
            tx["splits"] = cr
        wallets.sign_tx(tx, seller_wallet, chain_)
        return tx

    def expiring(self, chain_, within: int) -> list:
        """Open channels this seller was paid on that expire within `within` blocks."""
        h = len(chain_.blocks)
        with chain_.lock:
            return [cid for cid, c in chain_.ledger.channels.items()
                    if c["open"] and c["seller"] == self.seller and cid in self.book and c["expires"] - h <= within]


def _record_ok(line: str) -> bool:
    """A complete log record: '<64 hex channel> <digits> <64 hex word>', in hub mode
    followed by '<64 hex payee> <digits>'."""
    parts = line.split()
    if len(parts) not in (3, 5):
        return False
    ok = (ch.HEX64.match(parts[0]) is not None and parts[1].isascii() and parts[1].isdigit()
          and ch.HEX64.match(parts[2]) is not None)
    if len(parts) == 5:
        ok = ok and ch.HEX64.match(parts[3]) is not None and parts[4].isascii() and parts[4].isdigit()
    return ok


def refund_tx(chain_, wallet, cid: str) -> dict:
    """The buyer's channel_refund after expiry, if the seller never settled."""
    c = chain_.ledger.channels.get(cid)
    if c is None:
        raise ValueError("unknown channel")
    tx = chain_.new_tx(wallet.address, wallet.address, c["n"] * c["unit"], "channel:refund")
    tx.update(kind="channel_refund", channel=cid)
    wallets.sign_tx(tx, wallet, chain_)
    return tx
