"""Attacks on paymesh's ledger, signatures and paywall. Each test is an attack
that must FAIL against a correct system. Runs on a scratch chain and scratch
wallet directory, never the live chain.json.

    py -3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chain as ch  # noqa: E402
import paywall  # noqa: E402
import wallets  # noqa: E402


class Scratch(unittest.TestCase):
    """A fresh chain and wallet directory per test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._wd = wallets.WALLET_DIR
        wallets.WALLET_DIR = os.path.join(self.tmp.name, "wallets")
        self.c = ch.Chain(os.path.join(self.tmp.name, "chain.json"))
        self.c.genesis()

    def tearDown(self):
        wallets.WALLET_DIR = self._wd
        self.tmp.cleanup()

    def funded(self, name, amount):
        w = wallets.fund(self.c, name, amount)
        self.c.mine()
        return w

    def pay(self, w, to, amount, memo="pay"):
        tx = self.c.new_tx(w.address, to, amount, memo)
        wallets.sign_tx(tx, w, self.c)
        self.c.add_tx(tx)
        self.c.mine()
        return tx


class LedgerAttacks(Scratch):
    def test_overspend_is_refused(self):
        poor = self.funded("poor", 10)
        seller = self.funded("seller", 0)
        tx = self.c.new_tx(poor.address, seller.address, 1000, "too much")
        wallets.sign_tx(tx, poor, self.c)
        with self.assertRaises(ValueError):
            self.c.add_tx(tx)

    def test_overspend_across_pending_txs_is_refused(self):
        w = self.funded("w", 10)
        seller = self.funded("seller", 0)
        a = self.c.new_tx(w.address, seller.address, 7, "a")
        wallets.sign_tx(a, w, self.c)
        self.c.add_tx(a)  # pending, not mined
        b = self.c.new_tx(w.address, seller.address, 7, "b")
        wallets.sign_tx(b, w, self.c)
        with self.assertRaises(ValueError):
            self.c.add_tx(b)

    def test_negative_amount_cannot_pull_money(self):
        thief = self.funded("thief", 1)
        victim = self.funded("victim", 100)
        tx = self.c.new_tx(thief.address, victim.address, -100, "pull")
        wallets.sign_tx(tx, thief, self.c)
        with self.assertRaises(ValueError):
            self.c.add_tx(tx)
        self.assertEqual(self.c.balance(victim.address), 100)

    def test_zero_and_fractional_amounts_are_refused(self):
        w = self.funded("w", 10)
        for amt in (0, 1.5, True):
            tx = {"from_wallet": w.address, "to_wallet": "a" * 64, "amount": amt, "memo": "m", "txid": f"x{amt}"}
            with self.assertRaises(ValueError, msg=repr(amt)):
                self.c.add_tx(tx)

    def test_resubmitting_a_signed_tx_does_not_charge_twice(self):
        buyer = self.funded("buyer", 100)
        seller = self.funded("seller", 0)
        tx = self.pay(buyer, seller.address, 5)
        with self.assertRaises(ValueError):
            self.c.add_tx(json.loads(json.dumps(tx)))
        self.assertEqual(self.c.balance(buyer.address), 95)

    def test_a_block_cannot_mint_more_than_the_faucet_limit(self):
        with self.assertRaises(ValueError):
            self.c.add_tx(self.c.new_tx(ch.FAUCET, "ab" * 32, ch.FAUCET_MAX + 1, "faucet:big"))

    def test_tampered_chain_file_is_detected_on_load(self):
        buyer = self.funded("buyer", 100)
        seller = self.funded("seller", 0)
        self.pay(buyer, seller.address, 5)
        with open(self.c.path) as f:
            lines = f.read().splitlines()
        last = json.loads(lines[-1])
        last["txs"][0]["amount"] = 1  # rewrite history
        lines[-1] = json.dumps(last)
        with open(self.c.path, "w") as f:
            f.write("\n".join(lines) + "\n")
        with self.assertRaises(ValueError):
            ch.Chain(self.c.path)

    def test_a_block_cut_off_by_a_crash_is_dropped_cleanly(self):
        buyer = self.funded("buyer", 100)
        seller = self.funded("seller", 0)
        self.pay(buyer, seller.address, 5)
        good = ch.Chain(self.c.path)
        with open(self.c.path, "a") as f:
            f.write('{"index": 99, "timestamp": 1, "tx')  # power cut mid-append
        again = ch.Chain(self.c.path)
        self.assertEqual(len(again.blocks), len(good.blocks))
        self.assertEqual(again.balance(buyer.address), 95)
        again.mine()  # appends after the trimmed line, still loadable
        self.assertEqual(len(ch.Chain(self.c.path).blocks), len(good.blocks) + 1)

    def test_a_format_2_chain_is_upgraded_in_place(self):
        buyer = self.funded("buyer", 100)
        with open(self.c.path, "w") as f:
            json.dump({"format": 2, "blocks": [b.to_dict() for b in self.c.blocks]}, f)
        up = ch.Chain(self.c.path)
        self.assertEqual(up.balance(buyer.address), 100)
        with open(self.c.path) as f:
            self.assertEqual(json.loads(f.readline()), {"format": ch.FORMAT})


class SignatureAttacks(Scratch):
    def forge(self, victim_addr, to, tries=20000):
        """Forge a tx from victim_addr using only signatures published on the chain:
        for each key the victim used, hash its revealed WOTS chains forward."""
        by_key = {}
        for tx in self.c.all_txs():
            if tx.get("from_wallet") == victim_addr and tx.get("sig"):
                by_key.setdefault(wallets.signer_key(tx), []).append((wallets._wots_digits(wallets._msg_digest(tx)), tx["sig"]))
        for revealed in by_key.values():
            for n in range(tries // max(1, len(by_key))):
                tx = self.c.new_tx(victim_addr, to, 50, f"forged-{n}")
                tx.update(wallets.forge_fields(self.c, victim_addr))
                sig = wallets.wots_forge(tx, revealed)
                if sig is not None:
                    tx["sig"] = sig
                    return tx
        return None

    def test_paying_many_times_does_not_leak_the_key(self):
        victim = self.funded("victim", 100)
        seller = self.funded("seller", 0)
        for i in range(8):  # an agent making 8 paid calls
            self.pay(victim, seller.address, 1, f"call{i}")
        forged = self.forge(victim.address, "f" * 64)
        if forged is not None:
            with self.assertRaises(ValueError, msg="a forged tx built from published secrets was accepted"):
                self.c.add_tx(forged)
        self.assertEqual(self.c.balance(victim.address), 92)

    def test_a_key_never_signs_twice(self):
        w = self.funded("w", 100)
        seller = self.funded("seller", 0)
        a = self.pay(w, seller.address, 1, "a")
        b = self.pay(w, seller.address, 1, "b")
        self.assertNotEqual(wallets.signer_key(a), wallets.signer_key(b))


class PaywallAttacks(Scratch):
    class H(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    def test_malformed_memo_is_refused_not_crash(self):
        w = self.funded("w", 100)
        seller = self.funded("seller", 0)
        tx = self.pay(w, seller.address, 5, "no-colon-here")
        ok, why = paywall.verify_payment(self.c, self.H({"X-Payment": tx["txid"]}), "/premium/price")
        self.assertFalse(ok)

    def test_a_wrong_payment_does_not_burn_the_real_challenge(self):
        buyer = self.funded("buyer", 100)
        seller = self.funded("seller", 0)
        chal = paywall.open_challenge(self.c, seller.address, 5, "/premium/price")
        # an attacker pays 1 PMX against the buyer's challenge nonce first
        att = self.funded("att", 10)
        bad = self.pay(att, seller.address, 1, chal["memo"])
        ok, _ = paywall.verify_payment(self.c, self.H({"X-Payment": bad["txid"]}), "/premium/price")
        self.assertFalse(ok)
        good = self.pay(buyer, seller.address, 5, chal["memo"])
        ok2, why2 = paywall.verify_payment(self.c, self.H({"X-Payment": good["txid"]}), "/premium/price")
        self.assertTrue(ok2, why2)

    def test_challenge_flood_is_bounded(self):
        seller = self.funded("seller", 0)
        old = paywall.MAX_OPEN
        paywall.MAX_OPEN = 300
        try:
            for _ in range(paywall.MAX_OPEN + 500):
                paywall.open_challenge(self.c, seller.address, 5, "/premium/price")
            self.assertLessEqual(len(paywall._open_challenges), paywall.MAX_OPEN)
        finally:
            paywall.MAX_OPEN = old


class WalletNameAttacks(Scratch):
    def test_wallet_names_cannot_escape_the_wallet_dir(self):
        for bad in ("../evil", "..\\evil", "a/b", "", "x" * 100, "con."):
            with self.assertRaises(ValueError, msg=bad):
                wallets.load_or_create(bad)


if __name__ == "__main__":
    unittest.main()
