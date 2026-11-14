"""Fees: every signed tx burns 1 PMX per started 8 KB, signed over; blocks stamped before
FEE_EPOCH replay without fees; PMX is conserved (minted = held + locked + burned)."""
import os
import secrets
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chain as ch  # noqa: E402
import channels  # noqa: E402
import wallets  # noqa: E402
from test_attacks import Scratch  # noqa: E402


class Fees(Scratch):
    def signed(self, w, amount=1, memo="m", fee=None, **extra):
        tx = self.c.new_tx(w.address, "c" * 64, amount, memo)
        tx.update(extra)
        if fee is not None:
            tx["fee"] = fee
        wallets.sign_tx(tx, w, self.c)
        return tx

    def minted(self):
        return sum(t["amount"] for t in self.c.all_txs() if t["from_wallet"] == ch.FAUCET)

    def conserved(self):
        lg = self.c.ledger
        locked = sum(c["n"] * c["unit"] for c in lg.channels.values() if c["open"])
        self.assertEqual(sum(lg.balances.values()) + locked + lg.burned, self.minted())

    def test_fee_grows_with_size(self):
        w = self.funded("w", 100)
        self.assertEqual(self.signed(w)["fee"], 1)  # a WOTS v2 pay tx: ~5 KB
        lam = wallets.Wallet("lam", secrets.token_bytes(32), 0, None, None)  # an old Lamport key: ~52 KB
        tx = self.c.new_tx(lam.address, "c" * 64, 1, "m")
        wallets.sign_tx(tx, lam, self.c)
        self.assertEqual(tx["fee"], -(-wallets.tx_size(tx) // wallets.FEE_BYTES))
        self.assertGreaterEqual(tx["fee"], 6)
        self.assertEqual(wallets.fee_for(tx), tx["fee"])

    def test_too_low_missing_or_odd_fees_are_refused(self):
        w = self.funded("w", 100)
        for fee in (0, -1, 0.5, "1", True):
            with self.assertRaises(ValueError, msg=repr(fee)):
                self.c.add_tx(self.signed(w, fee=fee))
        nofee = self.c.new_tx(w.address, "c" * 64, 1, "m")
        nofee["fee"] = 1
        wallets.sign_tx(nofee, w, self.c)
        del nofee["fee"]  # (and the signature no longer matches either)
        with self.assertRaises(ValueError):
            self.c.add_tx(nofee)
        self.assertEqual(self.c.balance(w.address), 100)

    def test_the_fee_is_signed(self):
        w = self.funded("w", 100)
        tx = self.signed(w, fee=3)
        with self.assertRaises(ValueError):
            self.c.add_tx(dict(tx, fee=1))  # lowered after signing
        self.c.add_tx(tx)  # a fee above the minimum is fine
        self.c.mine()
        self.assertEqual((self.c.balance(w.address), self.c.ledger.burned), (96, 3))

    def test_the_fee_must_be_held_too(self):
        w = self.funded("w", 5)
        with self.assertRaises(ValueError) as cm:
            self.c.add_tx(self.signed(w, amount=5))
        self.assertIn("fee", str(cm.exception))
        self.c.add_tx(self.signed(w, amount=4, memo="fits"))
        self.c.mine()
        self.assertEqual(self.c.balance(w.address), 0)

    def test_pmx_is_conserved_through_channels_and_a_hub(self):
        buyer, seller, hub = self.funded("buyer", 300), self.funded("seller", 0), self.funded("hub", 0)
        self.c.add_tx(self.signed(buyer, amount=7))
        bc = channels.BuyerChannel(50)
        self.c.add_tx(bc.open_tx(self.c, buyer, seller.address, 1, 30))
        self.c.mine()
        book = channels.SellerBook(os.path.join(self.tmp.name, "s.log"), seller.address)
        for _ in range(4):
            book.accept(self.c, bc.pay(3), 3)
        self.c.add_tx(book.close_tx(self.c, seller, bc.id))
        hb = channels.BuyerChannel(40)
        self.c.add_tx(hb.open_tx(self.c, buyer, hub.address, 1, 30))
        self.c.mine()
        self.conserved()
        hbook = channels.SellerBook(os.path.join(self.tmp.name, "h.log"), hub.address)
        hbook.accept(self.c, hb.pay(2), 2, payee="a1" * 32)
        hbook.accept(self.c, hb.pay(5), 5, payee="b2" * 32)
        self.c.add_tx(hbook.close_tx(self.c, hub, hb.id))
        self.c.mine()
        self.conserved()
        self.assertEqual((self.c.balance("a1" * 32), self.c.balance("b2" * 32)), (2, 4))  # fee out of the largest
        self.assertEqual(self.c.ledger.burned, 5)  # pay, 2 opens, 2 closes
        again = ch.Chain(self.c.path)
        self.assertEqual(again.ledger.burned, 5)

    def test_blocks_before_the_epoch_replay_without_fees(self):
        old_epoch, old_set = wallets.FEE_EPOCH, wallets.set_fee
        try:
            wallets.FEE_EPOCH = 10**12  # "before fees"
            wallets.set_fee = lambda tx, kind="w2": tx.pop("fee", None)  # an old client: no fee field
            w = self.funded("old", 100)
            for n in range(3):
                self.c.add_tx(self.signed(w, memo=f"old{n}"))
                self.c.mine()
            self.assertNotIn("fee", self.c.all_txs()[-1])
            wallets.FEE_EPOCH = time.time() + 0.05  # fees start now
            time.sleep(0.1)
            with self.assertRaises(ValueError):
                self.c.add_tx(self.signed(w, memo="new, no fee"))
            wallets.set_fee = old_set
            self.c.add_tx(self.signed(w, memo="new, with fee"))
            self.c.mine()
            again = ch.Chain(self.c.path)  # the fee-less blocks still replay
            self.assertTrue(again.valid()[0])
            self.assertEqual(again.balance(w.address), 100 - 3 - 1 - 1)
        finally:
            wallets.FEE_EPOCH, wallets.set_fee = old_epoch, old_set


if __name__ == "__main__":
    unittest.main()
