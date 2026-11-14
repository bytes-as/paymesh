"""WOTS v2 (domain-separated hash chains): new keys use it, old wallets move to it at
their next signature, and every old signature (Lamport, WOTS v1) keeps verifying."""
import json
import os
import secrets
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chain as ch  # noqa: E402
import wallets  # noqa: E402
from test_attacks import Scratch  # noqa: E402


class WOTS2(Scratch):
    def old_wallet(self, name, version):
        """A wallet file as an older paymesh wrote it: v3 (all Lamport) or v4 (WOTS v1)."""
        seed = secrets.token_bytes(32)
        w = wallets.Wallet(name, seed, 0, None if version == 3 else 0, None)
        os.makedirs(wallets.WALLET_DIR, exist_ok=True)
        raw = {"name": name, "v": version, "address": w.address, "next_index": 0, "seed": seed.hex()}
        if version == 4:
            raw["wots_from"] = 0
        with open(w.path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        self.c.add_tx(self.c.new_tx(ch.FAUCET, w.address, 100, f"faucet:{name}"))
        self.c.mine()
        return wallets.load(name)

    def signed(self, w, to="c" * 64, amount=1, memo="m"):
        tx = self.c.new_tx(w.address, to, amount, memo)
        wallets.sign_tx(tx, w, self.c)
        return tx

    def test_new_wallets_sign_with_v2(self):
        w = self.funded("fresh", 100)
        tx = self.signed(w)
        self.assertEqual(len(tx["sig"]), wallets.WOTS2_SIG)
        self.assertEqual(wallets.signer_key(tx), w.address)
        self.c.add_tx(tx)
        self.c.mine()
        self.assertEqual(self.c.balance(w.address), 98)  # 1 + fee 1

    def test_v4_wallet_signs_once_more_as_v1_then_v2_and_history_reloads(self):
        w = self.old_wallet("old4", 4)
        sizes = []
        for n in range(3):
            tx = self.signed(w, memo=f"m{n}")
            sizes.append(len(tx["sig"]))
            self.c.add_tx(tx)
            self.c.mine()
        self.assertEqual(sizes, [wallets.WOTS_L, wallets.WOTS2_SIG, wallets.WOTS2_SIG])
        self.assertEqual(wallets.load("old4").wots2_from, 1)  # saved as v5
        again = ch.Chain(self.c.path)  # replays the v1 and the v2 signatures
        self.assertEqual(again.balance(w.address), 94)  # 3 x (1 + fee 1)
        self.assertTrue(again.valid()[0])

    def test_v3_lamport_wallet_moves_straight_to_v2(self):
        w = self.old_wallet("old3", 3)
        a, b = self.signed(w, memo="a"), None
        self.assertIn("pub", a)  # its current key is Lamport
        self.c.add_tx(a)
        self.c.mine()
        b = self.signed(w, memo="b")
        self.assertEqual(len(b["sig"]), wallets.WOTS2_SIG)
        self.c.add_tx(b)
        self.c.mine()
        self.assertTrue(ch.Chain(self.c.path).valid()[0])

    def test_v1_and_v2_signatures_cannot_be_swapped(self):
        w2 = self.funded("v2", 100)
        tx = self.signed(w2)
        as_v1 = dict(tx, sig=tx["sig"][1:])  # drop the public seed: read as v1
        self.assertFalse(wallets.verify_tx(as_v1, w2.address)[0])
        w1 = self.old_wallet("v1", 4)
        t1 = self.signed(w1)
        as_v2 = dict(t1, sig=[secrets.token_hex(32)] + t1["sig"])  # give a v1 signature a seed
        self.assertFalse(wallets.verify_tx(as_v2, w1.address)[0])
        self.assertTrue(wallets.verify_tx(t1, w1.address)[0])

    def test_values_revealed_by_one_key_are_useless_under_another_seed(self):
        a, b = self.funded("ka", 100), self.funded("kb", 100)
        ta = self.signed(a, memo="same")
        tb = self.c.new_tx(b.address, "c" * 64, 1, "same")
        tb.update(next_key=ta["next_key"], txid=ta["txid"], from_wallet=a.address)
        # key b's public seed with key a's revealed chain values (a multi-target forger's move)
        _, _, seed_b = wallets.derive_wots2(b.seed, 0)
        moved = dict(ta, sig=[seed_b.hex()] + ta["sig"][1:])
        self.assertNotEqual(wallets.signer_key(moved), wallets.signer_key(ta))
        self.assertFalse(wallets.verify_tx(moved, a.address)[0])
        self.assertFalse(wallets.verify_tx(moved, b.address)[0])

    def test_every_chain_step_is_a_different_hash(self):
        seed, x = b"s" * 32, b"x" * 32
        outs = {wallets._f2(seed, j, step, x) for j in range(3) for step in range(3)}
        self.assertEqual(len(outs), 9)
        self.assertNotEqual(wallets._f2(seed, 0, 0, x), wallets._f2(b"t" * 32, 0, 0, x))

    def test_forging_from_many_published_v2_signatures_fails(self):
        victim = self.funded("victim2", 100)
        revealed = []
        for n in range(8):
            tx = self.signed(victim, memo=f"call{n}")
            revealed.append((wallets.sig_digits(tx), tx["sig"]))
            self.c.add_tx(tx)
            self.c.mine()
        for n in range(3000):  # every key signed once: no key has two signatures to combine
            target = self.c.new_tx(victim.address, "f" * 64, 50, f"forged-{n}")
            target["next_key"] = "e" * 64
            sig = wallets.wots_forge(target, revealed[-1:])
            if sig is not None:
                target["sig"] = sig
                with self.assertRaises(ValueError):
                    self.c.add_tx(target)
        self.assertEqual(self.c.balance(victim.address), 84)


if __name__ == "__main__":
    unittest.main()
