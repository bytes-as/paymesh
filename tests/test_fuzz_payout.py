"""Fuzz the channel ledger with hub payouts: a random sequence of opens, ticks, payouts, closes,
refunds, expiries and crashes (chain and hub log reloaded from disk, pending txs lost, also in
the middle of a payout). After every step:

  * conservation: balances + burned fees + what channels still hold == everything minted;
  * per channel: paid_shares add up to paid_ticks * unit, paid_ticks <= the best word seen;
  * no tick paid twice: each payout/close settles only ticks past the last paid one, and no
    seller ever received more than the hub credited it on that channel;
  * a crash loses no mined block and no acknowledged credit.

When a channel ends: its deposit == what its sellers got + its fees + what went back to the buyer.
PAYMESH_FUZZ_STEPS (default 250) and PAYMESH_FUZZ_SEED set the run; long runs: thousands of steps.
"""
import collections
import os
import random
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chain as ch  # noqa: E402
import channels  # noqa: E402
import durable  # noqa: E402
import wallets  # noqa: E402

STEPS = int(os.environ.get("PAYMESH_FUZZ_STEPS", 250))
SEED = int(os.environ.get("PAYMESH_FUZZ_SEED", 20261115))
PAYEES = [f"{k:02x}" * 32 for k in (0xa1, 0xb2, 0xc3, 0xd4)]


class PayoutFuzz(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._wd, self._win = wallets.WALLET_DIR, ch.REFUND_WINDOW_S
        wallets.WALLET_DIR = os.path.join(self.tmp.name, "wallets")
        ch.REFUND_WINDOW_S = 0  # the window is tested elsewhere; here the claim may follow at once
        self.path = os.path.join(self.tmp.name, "chain.json")
        self.c = ch.Chain(self.path)
        self.c.genesis()
        self.minted = 0
        self.hub = wallets.fund(self.c, "hub", 0)
        self.buyers = [self.mint(f"buyer{k}", 600) for k in range(3)]
        self.c.mine()
        self.book = channels.SellerBook(os.path.join(self.tmp.name, "hub.log"), self.hub.address)
        self.model = {}  # channel id -> {"bc", "buyer", "credits": {payee: PMX}}
        self.seen = 0  # blocks folded into the per-channel tallies below
        self.got = collections.defaultdict(lambda: collections.defaultdict(int))  # cid -> payee -> PMX received
        self.fees = collections.defaultdict(int)  # cid -> fees of its payouts and close
        self.back = collections.defaultdict(int)  # cid -> what went back to the buyer
        self.settled_to = collections.defaultdict(int)  # cid -> ticks paid by its payouts/close so far
        self.ended = set()
        self.ops = collections.Counter()

    def tearDown(self):
        wallets.WALLET_DIR, ch.REFUND_WINDOW_S = self._wd, self._win
        durable.close_all()
        self.tmp.cleanup()

    # --- helpers ---
    def mint(self, name, amount):
        self.minted += amount
        return wallets.fund(self.c, name, amount)

    def mine_with(self, tx):
        self.c.add_tx(tx)
        self.c.mine()

    def open_channels(self):
        return [cid for cid in self.model if self.c.ledger.channels[cid]["open"]]

    def crash(self):
        """Process death: pending txs are gone; chain and hub log come back from disk."""
        blocks, balances = len(self.c.blocks), dict(self.c.ledger.balances)
        credits = {cid: dict(v) for cid, v in self.book.credits.items()}
        self.book.close()
        self.c = ch.Chain(self.path)
        self.book = channels.SellerBook(self.book.path, self.hub.address)
        self.assertEqual(len(self.c.blocks), blocks, "a crash lost a mined block")
        self.assertEqual(self.c.ledger.balances, balances)
        self.assertEqual(self.book.credits, credits, "a crash lost an acknowledged credit")

    # --- the random steps ---
    def step(self, rng):
        op = rng.choices(["open", "tick", "tick", "tick", "payout", "payout_crash", "crash", "close",
                          "refund", "blocks", "replay"], weights=[3, 8, 8, 8, 5, 2, 1, 2, 2, 3, 1])[0]
        live = self.open_channels()
        if op == "open" and len(live) < 6:
            b = rng.choice(self.buyers)
            if self.c.balance(b.address) < 150:
                self.mint(b.name, 300)
                self.c.mine()
            bc = channels.BuyerChannel(rng.randint(5, 60))
            self.mine_with(bc.open_tx(self.c, b, self.hub.address, rng.choice([1, 2]),
                                      rng.randint(channels.SellerBook.STOP_WITHIN + 3, 90)))
            self.model[bc.id] = {"bc": bc, "buyer": b, "credits": collections.Counter()}
        elif op == "tick" and live:
            cid = rng.choice(live)
            m, chan = self.model[cid], self.c.ledger.channels[cid]
            k = rng.randint(1, 3)
            if m["bc"].spent + k > m["bc"].n:
                return
            payee = rng.choice(PAYEES)
            ok, _ = self.book.accept(self.c, m["bc"].pay(k), k * chan["unit"], payee=payee)
            if ok:
                m["credits"][payee] += k * chan["unit"]
            else:
                m["bc"].refund(k)  # e.g. too close to expiry: the word was not taken
        elif op in ("payout", "payout_crash") and live:
            cid = rng.choice(live)
            try:
                tx = self.book.payout_tx(self.c, self.hub, cid)
            except ValueError:
                return  # nothing new, or not worth the fee
            self.c.add_tx(tx)
            if op == "payout_crash":
                self.crash()  # died before mining it: the next payout recomputes from the chain
            else:
                self.c.mine()
        elif op == "crash":
            self.crash()
        elif op == "close" and live:
            cid = rng.choice(live)
            try:
                self.mine_with(self.book.close_tx(self.c, self.hub, cid))
            except ValueError:
                return
        elif op == "refund":
            due = [cid for cid in live if self.c.height >= self.c.ledger.channels[cid]["expires"]]
            if due:
                cid = rng.choice(due)
                self.mine_with(channels.refund_tx(self.c, self.model[cid]["buyer"], cid))  # request, then claim
        elif op == "blocks":
            for _ in range(rng.randint(1, 8)):
                self.c.mine()
        elif op == "replay":
            pays = [t for b in self.c.blocks[-40:] for t in b.txs if t.get("kind") == "channel_payout"
                    and self.c.ledger.channels[t["channel"]]["open"]]
            if pays:
                old = rng.choice(pays)
                with self.assertRaises(ValueError):
                    self.c.add_tx(old)  # the mined tx itself
                again = self.c.new_tx(self.hub.address, self.hub.address, old["amount"], "channel:payout")
                again.update({k: old[k] for k in ("kind", "channel", "ticks", "word", "shares", "splits")})
                wallets.sign_tx(again, self.hub, self.c)
                with self.assertRaises(ValueError):
                    self.c.add_tx(again)  # the same ticks, signed anew
        else:
            return
        self.ops[op] += 1

    # --- the invariants ---
    def fold_new_blocks(self):
        for blk in self.c.blocks[self.seen:]:
            for tx in blk.txs:
                cid, kind = tx.get("channel"), tx.get("kind")
                if cid not in self.model:
                    continue
                chan, fee = self.c.ledger.channels[cid], tx.get("fee", 0)
                if kind in ("channel_payout", "channel_close"):
                    self.fees[cid] += fee  # out of the deposit (a refund's fees are not: see below)
                    new = tx["ticks"] - self.settled_to[cid]
                    self.assertGreaterEqual(new, 0 if kind == "channel_close" else 1, f"{kind} re-paid ticks")
                    self.settled_to[cid] = tx["ticks"]
                    for who, part in (tx.get("splits") or {}).items():
                        self.got[cid][who] += part
                    if kind == "channel_close":
                        rest = chan["n"] * chan["unit"] - tx["amount"]
                        self.back[cid] += rest - (fee if new == 0 else 0)
                elif kind == "channel_refund" and self._is_claim(cid, tx):
                    # the claim returns `amount` (its fee comes out of it); the request's fee came
                    # from the buyer's balance, not from the deposit
                    self.back[cid] += tx["amount"]
        self.seen = len(self.c.blocks)

    def _is_claim(self, cid, tx):
        """The second channel_refund of a channel (the claim) returns money; the first only opens the window."""
        refunds = [t for b in self.c.blocks for t in b.txs if t.get("channel") == cid and t.get("kind") == "channel_refund"]
        return len(refunds) >= 2 and refunds[1]["txid"] == tx["txid"]

    def check(self):
        led = self.c.ledger
        held = sum((c["n"] - c.get("paid_ticks", 0)) * c["unit"] for c in led.channels.values() if c["open"])
        self.assertEqual(sum(led.balances.values()) + led.burned + held, self.minted, "PMX created or lost")
        self.fold_new_blocks()
        for cid, m in self.model.items():
            c = led.channels[cid]
            paid = c.get("paid_ticks", 0)
            self.assertEqual(sum((c.get("paid_shares") or {}).values()), paid * c["unit"])
            best = self.book.best(cid)
            self.assertLessEqual(paid, best["i"] if best else 0)
            self.assertEqual(dict(self.book.credits.get(cid, {})), dict(m["credits"]))
            for who, amt in self.got[cid].items():
                self.assertLessEqual(amt, m["credits"][who], f"{who[:8]} got more than it earned")
            if not c["open"] and cid not in self.ended:
                self.ended.add(cid)
                deposit = c["n"] * c["unit"]
                self.assertEqual(sum(self.got[cid].values()) + self.fees[cid] + self.back[cid], deposit,
                                 f"channel {cid[:8]}: sellers + fees + buyer's rest != deposit")

    def test_random_opens_ticks_payouts_closes_and_crashes(self):
        rng = random.Random(SEED)
        t0 = time.time()
        for _ in range(STEPS):
            self.step(rng)
            self.check()
        for cid in self.open_channels():  # wind down: every channel ends and balances out
            try:
                self.mine_with(self.book.close_tx(self.c, self.hub, cid))
            except ValueError:  # nothing ever paid on it: the buyer refunds
                while self.c.height < self.c.ledger.channels[cid]["expires"]:
                    self.c.mine()
                for _ in range(2):  # request + claim (or only the claim, if it was requested before)
                    if self.c.ledger.channels[cid]["open"]:
                        self.mine_with(channels.refund_tx(self.c, self.model[cid]["buyer"], cid))
            self.check()
        self.assertEqual(self.open_channels(), [])
        self.assertTrue(ch.Chain(self.path).valid()[0])
        self.assertEqual(len(self.ended), len(self.model))
        if os.environ.get("PAYMESH_FUZZ_REPORT"):
            print(f"\nfuzz: {STEPS} steps, seed {SEED}, {time.time() - t0:.0f} s, {len(self.model)} channels, "
                  f"{len(self.c.blocks)} blocks; ops {dict(self.ops)}; invariants held after every step")


if __name__ == "__main__":
    unittest.main()
