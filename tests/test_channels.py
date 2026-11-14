"""Payment channels: the honest flow settles exactly, and every cheat fails."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chain as ch  # noqa: E402
import channels  # noqa: E402
import wallets  # noqa: E402
from test_attacks import Scratch  # noqa: E402


class ChannelBase(Scratch):
    def open(self, ticks=100, unit=1, expires_in=50, deposit_from="buyer", fund=500):
        self.buyer = self.funded(deposit_from, fund)
        self.seller = self.funded("seller", 0)
        self.bc = channels.BuyerChannel(ticks)
        tx = self.bc.open_tx(self.c, self.buyer, self.seller.address, unit, expires_in)
        self.c.add_tx(tx)
        self.c.mine()
        self.book = channels.SellerBook(os.path.join(self.tmp.name, "book.json"), self.seller.address)
        return tx


class ChannelLedger(ChannelBase):
    def test_honest_flow_settles_exactly(self):
        self.open(ticks=100)
        self.assertEqual(self.c.balance(self.buyer.address), 399)  # 100 locked, 1 fee
        for _ in range(7):  # 7 calls at 5 PMX
            ok, why = self.book.accept(self.c, self.bc.pay(5), 5)
            self.assertTrue(ok, why)
        tx = self.book.close_tx(self.c, self.seller, self.bc.id)
        self.c.add_tx(tx)
        self.c.mine()
        self.assertEqual(self.c.balance(self.seller.address), 34)  # 35, the close's fee out of it
        self.assertEqual(self.c.balance(self.buyer.address), 464)
        self.assertTrue(self.c.valid()[0])
        self.assertTrue(ch.Chain(self.c.path).valid()[0])  # reloads clean

    def test_seller_cannot_claim_unrevealed_ticks(self):
        self.open(ticks=100)
        self.book.accept(self.c, self.bc.pay(3), 3)
        for i, w in ((50, "ab" * 32), (3, "cd" * 32), (4, self.bc.words[3])):
            tx = self.c.new_tx(self.seller.address, self.buyer.address, i, "channel:close")
            tx.update(kind="channel_close", channel=self.bc.id, ticks=i, word=w)
            wallets.sign_tx(tx, self.seller, self.c)
            with self.assertRaises(ValueError, msg=i):
                self.c.add_tx(tx)

    def test_only_the_seller_closes_and_only_once(self):
        self.open()
        self.book.accept(self.c, self.bc.pay(10), 10)
        cid, word = self.bc.id, self.bc.words[10]
        fake = self.c.new_tx(self.buyer.address, self.buyer.address, 10, "channel:close")  # buyer closes
        fake.update(kind="channel_close", channel=cid, ticks=10, word=word)
        wallets.sign_tx(fake, self.buyer, self.c)
        with self.assertRaises(ValueError):
            self.c.add_tx(fake)
        self.c.add_tx(self.book.close_tx(self.c, self.seller, cid))
        self.c.mine()
        with self.assertRaises(ValueError):
            self.book.close_tx(self.c, self.seller, cid)
        with self.assertRaises(ValueError):
            self.c.add_tx(channels.refund_tx(self.c, self.buyer, cid))

    def test_refund_only_after_expiry(self):
        self.open(expires_in=5)
        with self.assertRaises(ValueError):
            self.c.add_tx(channels.refund_tx(self.c, self.buyer, self.bc.id))
        for _ in range(6):
            self.c.mine()
        old = ch.REFUND_WINDOW_S
        ch.REFUND_WINDOW_S = 0  # the window itself is tested in test_expiry
        try:
            self.c.add_tx(channels.refund_tx(self.c, self.buyer, self.bc.id))  # 1: opens the window
            self.c.mine()
            self.assertEqual(self.c.balance(self.buyer.address), 398)  # deposit still held; request fee paid
            self.c.add_tx(channels.refund_tx(self.c, self.buyer, self.bc.id))  # 2: the claim
            self.c.mine()
        finally:
            ch.REFUND_WINDOW_S = old
        self.assertEqual(self.c.balance(self.buyer.address), 497)  # fees: open, request, claim

    def test_open_needs_funds_and_exact_deposit(self):
        buyer = self.funded("poor", 10)
        seller = self.funded("seller", 0)
        bc = channels.BuyerChannel(100)
        with self.assertRaises(ValueError):
            self.c.add_tx(bc.open_tx(self.c, buyer, seller.address, 1))
        tx = channels.BuyerChannel(5).open_tx(self.c, buyer, seller.address, 1)
        tx["amount"] = 1  # deposit below ticks*unit (and the signature no longer matches)
        with self.assertRaises(ValueError):
            self.c.add_tx(tx)

    def test_signed_channel_terms_cannot_be_altered(self):
        self.buyer = self.funded("buyer", 500)
        seller = self.funded("seller", 0)
        bc = channels.BuyerChannel(10)
        tx = bc.open_tx(self.c, self.buyer, seller.address, 1)
        tx["anchor"] = "f" * 64  # a relay swaps the anchor for one it controls
        with self.assertRaises(ValueError):
            self.c.add_tx(tx)


class SellerChecks(ChannelBase):
    def test_replay_rewind_underpay_and_foreign_words_fail(self):
        self.open(ticks=50)
        h1 = self.bc.pay(5)
        self.assertTrue(self.book.accept(self.c, h1, 5)[0])
        self.assertFalse(self.book.accept(self.c, h1, 5)[0], "replayed header accepted")
        cid = self.bc.id
        self.assertFalse(self.book.accept(self.c, f"{cid} 3 {self.bc.words[3]}", 1)[0], "rewound tick accepted")
        self.assertFalse(self.book.accept(self.c, f"{cid} 7 {self.bc.words[7]}", 5)[0], "underpayment accepted")
        self.assertFalse(self.book.accept(self.c, f"{cid} 20 {'ab' * 32}", 5)[0], "word off the chain accepted")
        self.assertFalse(self.book.accept(self.c, f"{'cd' * 32} 1 {'ab' * 32}", 1)[0], "unknown channel accepted")
        self.assertFalse(self.book.accept(self.c, "garbage", 1)[0])

    def test_seller_record_survives_restart(self):
        self.open(ticks=50)
        h = self.bc.pay(5)
        self.assertTrue(self.book.accept(self.c, h, 5)[0])
        fresh = channels.SellerBook(self.book.path, self.seller.address)
        self.assertFalse(fresh.accept(self.c, h, 5)[0], "a restarted seller took a spent word again")

    def test_channel_to_another_seller_is_refused(self):
        self.open()
        other = channels.SellerBook(os.path.join(self.tmp.name, "other.json"), "9" * 64)
        self.assertFalse(other.accept(self.c, self.bc.pay(1), 1)[0])


if __name__ == "__main__":
    unittest.main()
