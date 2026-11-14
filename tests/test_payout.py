"""Hub payouts (channel_payout): sellers are paid what buyers revealed on a hub channel while
the channel stays open; the chain keeps paid_ticks / paid_word / paid_shares, so no tick is
paid twice and close and refund move only what is left."""
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chain as ch  # noqa: E402
import channels  # noqa: E402
import server  # noqa: E402
import wallets  # noqa: E402
from paymesh_client import Client  # noqa: E402
from test_attacks import Scratch  # noqa: E402
from test_e2e import free_port  # noqa: E402

A, B = "a1" * 32, "b2" * 32


class Payouts(Scratch):
    def open_hub(self, ticks=100, expires=200):
        self.buyer = self.funded("buyer", 500)
        self.hub = self.funded("hub", 0)
        self.bc = channels.BuyerChannel(ticks)
        self.c.add_tx(self.bc.open_tx(self.c, self.buyer, self.hub.address, 1, expires))
        self.c.mine()
        self.book = channels.SellerBook(os.path.join(self.tmp.name, "hub.log"), self.hub.address)
        self.b0 = self.c.balance(self.buyer.address)
        return self.book

    def take(self, payee, ticks):
        ok, why = self.book.accept(self.c, self.bc.pay(ticks), ticks, payee=payee)
        self.assertTrue(ok, why)

    def mine(self, tx):
        self.c.add_tx(tx)
        self.c.mine()
        return tx

    def payout(self):
        return self.mine(self.book.payout_tx(self.c, self.hub, self.bc.id))

    def chan(self):
        return self.c.ledger.channels[self.bc.id]

    def test_payouts_then_close_pay_each_tick_once(self):
        self.open_hub()
        for _ in range(5):
            self.take(A, 2)
        self.take(B, 6)
        p1 = self.payout()  # 16 PMX: A 10, B 6, the 1 PMX fee out of the largest share
        self.assertEqual((p1["amount"], p1["shares"], p1["splits"]), (16, {A: 10, B: 6}, {A: 9, B: 6}))
        self.assertEqual((self.c.balance(A), self.c.balance(B)), (9, 6))
        self.assertEqual((self.chan()["open"], self.chan()["paid_ticks"], self.chan()["paid_shares"]), (True, 16, {A: 10, B: 6}))
        self.take(A, 4)
        p2 = self.payout()
        self.assertEqual((p2["amount"], p2["splits"]), (4, {A: 3}))
        self.take(B, 3)
        close = self.mine(self.book.close_tx(self.c, self.hub, self.bc.id))
        self.assertEqual((close["amount"], close["splits"]), (23, {B: 2}))  # cumulative amount, only the new 3 split
        self.assertEqual((self.c.balance(A), self.c.balance(B)), (12, 8))
        back = self.c.balance(self.buyer.address) - self.b0
        self.assertEqual(back, 100 - 23)
        self.assertEqual(12 + 8 + 3 + back, 100)  # sellers + three fees + the buyer's rest = the deposit
        self.assertFalse(self.chan()["open"])
        self.assertTrue(ch.Chain(self.c.path).valid()[0])  # and it all replays

    def test_no_tick_is_paid_twice(self):
        self.open_hub()
        self.take(A, 5)
        p1 = self.payout()
        with self.assertRaises(ValueError):  # nothing new
            self.book.payout_tx(self.c, self.hub, self.bc.id)
        again = self.c.new_tx(self.hub.address, self.hub.address, 5, "channel:payout")
        again.update(kind="channel_payout", channel=self.bc.id, ticks=5, word=p1["word"], shares={A: 5}, splits={A: 4})
        wallets.sign_tx(again, self.hub, self.c)
        with self.assertRaises(ValueError):  # the same ticks, re-signed
            self.c.add_tx(again)
        with self.assertRaises(ValueError):  # the mined tx itself, replayed
            self.c.add_tx(p1)
        self.take(B, 4)
        first = self.book.payout_tx(self.c, self.hub, self.bc.id)
        twin = self.book.payout_tx(self.c, self.hub, self.bc.id)  # built from the same state (a retry)
        self.c.add_tx(first)
        with self.assertRaises(ValueError):  # both pending at once: the second is refused
            self.c.add_tx(twin)
        self.c.mine()
        self.assertEqual((self.c.balance(A), self.c.balance(B)), (4, 3))

    def test_payout_rules(self):
        self.open_hub()
        self.take(A, 6)
        good = self.book.payout_tx(self.c, self.hub, self.bc.id)

        def variant(**kw):
            t = self.c.new_tx(self.hub.address, kw.pop("to", self.hub.address), kw.pop("amount", 6), "channel:payout")
            t.update(dict(dict(kind="channel_payout", channel=self.bc.id, ticks=6, word=good["word"],
                               shares={A: 6}, splits={A: 5}), **kw))
            t = {k: v for k, v in t.items() if v is not None}
            wallets.sign_tx(t, self.hub, self.c)
            return t
        bad = {
            "shares do not add up": variant(shares={A: 5}),
            "split above its share": variant(shares={A: 3, B: 3}, splits={A: 5}),
            "split to a payee without a share": variant(splits={B: 5}),
            "no splits": variant(splits=None),
            "no shares": variant(shares=None),
            "more ticks than the word": variant(ticks=7, amount=7, shares={A: 7}, splits={A: 6}),
            "a word off the chain": variant(word="ee" * 32),
            "paid to the buyer": variant(to=self.buyer.address),
        }
        for why, t in bad.items():
            with self.assertRaises(ValueError, msg=why):
                self.c.add_tx(t)
        forged = self.c.new_tx(self.buyer.address, self.buyer.address, 6, "channel:payout")
        forged.update(kind="channel_payout", channel=self.bc.id, ticks=6, word=good["word"], shares={A: 6}, splits={A: 5})
        wallets.sign_tx(forged, self.buyer, self.c)
        with self.assertRaises(ValueError):  # only the channel's seller pays out
            self.c.add_tx(forged)
        with self.assertRaises(ValueError):  # shares rewritten after the hub signed
            self.c.add_tx(dict(good, shares={B: 6}))
        self.mine(good)
        self.assertEqual(self.c.balance(A), 5)

    def test_close_after_everything_was_paid_out(self):
        self.open_hub()
        self.take(A, 10)
        self.payout()
        close = self.mine(self.book.close_tx(self.c, self.hub, self.bc.id))
        self.assertNotIn("splits", close)
        self.assertEqual(self.c.balance(A), 9)
        self.assertEqual(self.c.balance(self.buyer.address) - self.b0, 100 - 10 - close["fee"])  # its rest pays the fee
        self.assertFalse(self.chan()["open"])

    def test_refund_after_payouts_returns_only_the_rest(self):
        self.open_hub(expires=self.c.height + channels.SellerBook.STOP_WITHIN + 5)
        self.take(A, 30)
        self.payout()
        while self.c.height < self.chan()["expires"]:
            self.c.mine()
        full = self.c.new_tx(self.buyer.address, self.buyer.address, 100, "channel:refund")
        full.update(kind="channel_refund", channel=self.bc.id)
        wallets.sign_tx(full, self.buyer, self.c)
        with self.assertRaises(ValueError):  # the whole deposit: 30 of it was paid out
            self.c.add_tx(full)
        old = ch.REFUND_WINDOW_S
        ch.REFUND_WINDOW_S = 0
        try:
            r1 = self.mine(channels.refund_tx(self.c, self.buyer, self.bc.id))
            self.assertEqual(r1["amount"], 70)
            r2 = self.mine(channels.refund_tx(self.c, self.buyer, self.bc.id))
        finally:
            ch.REFUND_WINDOW_S = old
        self.assertEqual(self.c.balance(self.buyer.address) - self.b0, 70 - r1["fee"] - r2["fee"])
        self.assertEqual(self.c.balance(A), 29)

    def test_paying_out_the_whole_deposit_ends_the_channel(self):
        self.open_hub(ticks=10)
        self.take(A, 10)
        self.payout()
        self.assertFalse(self.chan()["open"])
        self.assertEqual(self.c.balance(A), 9)

    def test_a_restarted_hub_pays_only_what_is_new(self):
        self.open_hub()
        self.take(A, 5)
        self.take(B, 5)
        self.payout()
        self.book.close()
        self.book = channels.SellerBook(self.book.path, self.hub.address)  # credits from its log, payouts from the chain
        with self.assertRaises(ValueError):
            self.book.payout_tx(self.c, self.hub, self.bc.id)
        self.take(B, 3)
        p = self.payout()
        self.assertEqual(p["shares"], {B: 3})
        self.assertEqual((self.c.balance(A), self.c.balance(B)), (4, 5 + 2))


class NodeSchedule(unittest.TestCase):
    """The node pays hub sellers on its own: by time (HUB_PAYOUT_S) and by amount (HUB_PAYOUT_MIN)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls._wd = wallets.WALLET_DIR
        wallets.WALLET_DIR = os.path.join(cls.tmp.name, "wallets")
        cls.chain = ch.Chain.open_or_archive(os.path.join(cls.tmp.name, "chain.json"))
        cls.old = (server.HUB_PAYOUT_S, server.HUB_PAYOUT_MIN)
        server.HUB_PAYOUT_S, server.HUB_PAYOUT_MIN = 1.0, 10**9
        cls.port = free_port()
        cls.api = server.start(cls.chain, port=cls.port)
        cls.url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        server.HUB_PAYOUT_S, server.HUB_PAYOUT_MIN = cls.old
        cls.api.settler_stop.set()
        cls.api.shutdown()
        cls.api.server_close()
        wallets.WALLET_DIR = cls._wd
        cls.tmp.cleanup()

    def wait_for(self, cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.1)
        return False

    def test_sellers_are_paid_on_schedule_before_any_close(self):
        seller = server.SELLER.address
        c = Client(self.url, "payout-buyer")
        c.faucet()
        c.open_hub(ticks=60)
        start = self.chain.balance(seller)
        for _ in range(3):
            self.assertEqual(c.get("/premium/price")[0], 200)  # 5 PMX each, credited to the node's seller
        self.assertTrue(self.wait_for(lambda: self.chain.balance(seller) - start == 15 - 1), "no timed payout")
        chan = self.chain.ledger.channels[c.channel.id]
        self.assertEqual((chan["open"], chan["paid_ticks"]), (True, 15))
        server.HUB_PAYOUT_S, server.HUB_PAYOUT_MIN = 10**9, 10  # now only by amount
        try:
            c.get("/premium/price")  # 5 owed: below the minimum
            time.sleep(1.5)
            self.assertEqual(self.chain.balance(seller) - start, 14)
            c.get("/premium/price")  # 10 owed: paid at once
            self.assertTrue(self.wait_for(lambda: self.chain.balance(seller) - start == 14 + 9), "no payout at the minimum")
        finally:
            server.HUB_PAYOUT_S, server.HUB_PAYOUT_MIN = 1.0, 10**9
        cid = c.channel.id
        c.close_channel()
        self.assertEqual(self.chain.balance(seller) - start, 23)  # nothing new at the close: nothing paid twice
        self.assertFalse(self.chain.ledger.channels[cid]["open"])


if __name__ == "__main__":
    unittest.main()
