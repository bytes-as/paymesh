"""Channel expiry, at the exact boundary heights, on both payment paths (X-Channel and x402),
and the order of a close and a refund that meet in the same block."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import channels  # noqa: E402
import paymesh_x402 as x402  # noqa: E402
from test_attacks import Scratch  # noqa: E402

NET = "paymesh:" + "0" * 32


class Expiry(Scratch):
    def open(self, expires_in):
        self.buyer, self.seller = self.funded("buyer", 500), self.funded("seller", 0)
        self.bc = channels.BuyerChannel(100)
        self.c.add_tx(self.bc.open_tx(self.c, self.buyer, self.seller.address, 1, expires_in))
        self.c.mine()
        self.book = channels.SellerBook(os.path.join(self.tmp.name, "book.log"), self.seller.address)
        return self.c.ledger.channels[self.bc.id]["expires"]

    def native(self, price=1):
        return self.book.accept(self.c, self.bc.pay(price), price)[0]

    def via_x402(self, price=1):
        cid, i, w = self.bc.pay(price).split()
        acc = x402.requirement(NET, self.seller.address, price, "channel", 60, unit=1)
        sig = x402.encode({"x402Version": 2, "accepted": acc, "payload": {"channel": cid, "tick": int(i), "word": w}})
        status, _, _ = x402.settle(sig, network=NET, pay_to=self.seller.address, price=price, path="/p",
                                   chain_=self.c, book=self.book, submit=None)
        return status == 200

    def test_boundary_heights_on_both_paths(self):
        stop = channels.SellerBook.STOP_WITHIN
        exp = self.open(stop + 3)
        seen = {}
        while self.c.height <= exp + 1:  # from 3 blocks outside the cutoff to past expiry
            left = exp - self.c.height
            for path, pay in (("native", self.native), ("x402", self.via_x402)):
                ok = pay()
                if not ok:
                    self.bc.refund(1)  # the word wasn't taken: the buyer may reuse it
                seen[(path, left)] = ok
            self.c.mine()
        for (path, left), ok in seen.items():
            self.assertEqual(ok, left > stop, f"{path}: {left} blocks before expiry")
        self.assertTrue(any(seen.values()) and not all(seen.values()))

    def test_close_and_refund_in_one_block_first_one_wins(self):
        self.open(channels.SellerBook.STOP_WITHIN + 3)
        self.native(3)
        while self.c.height < self.c.ledger.channels[self.bc.id]["expires"]:
            self.c.mine()
        close = self.book.close_tx(self.c, self.seller, self.bc.id)
        self.c.add_tx(close)  # pending, not yet mined
        with self.assertRaises(ValueError):  # the refund lands after the close in the same block: refused
            self.c.add_tx(channels.refund_tx(self.c, self.buyer, self.bc.id))
        self.c.mine()
        self.assertEqual(self.c.ledger.channels[self.bc.id]["paid"], 3)

    def test_refund_first_then_the_close_is_refused(self):
        self.open(5)
        while self.c.height < self.c.ledger.channels[self.bc.id]["expires"]:
            self.c.mine()
        self.c.add_tx(channels.refund_tx(self.c, self.buyer, self.bc.id))  # nothing was paid: refund is right
        self.book.book[self.bc.id] = {"i": 2, "w": self.bc.words[2]}  # a seller claiming a word after the fact
        with self.assertRaises(ValueError):
            self.c.add_tx(self.book.close_tx(self.c, self.seller, self.bc.id))
        self.c.mine()
        self.assertEqual(self.c.ledger.channels[self.bc.id]["paid"], 0)


if __name__ == "__main__":
    unittest.main()
