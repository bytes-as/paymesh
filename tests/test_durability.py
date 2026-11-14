"""Crash recovery: files cut at every point of their last record, and a real writer
process killed mid-run again and again. Nothing acknowledged is lost, a torn record is
never taken as data, and the next append after a crash survives the next reload."""
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chain as ch  # noqa: E402
import channels  # noqa: E402
import durable  # noqa: E402
import wallets  # noqa: E402

SELLER, CID = "aa" * 32, "cc" * 32


class TornTail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._wd = wallets.WALLET_DIR
        wallets.WALLET_DIR = os.path.join(self.tmp.name, "wallets")

    def tearDown(self):
        wallets.WALLET_DIR = self._wd
        durable.close_all()
        self.tmp.cleanup()

    def chain_file(self, blocks=4):
        c = ch.Chain(os.path.join(self.tmp.name, "chain.json"))
        c.genesis()
        for _ in range(blocks - 1):
            c.add_tx(c.new_tx(ch.FAUCET, "bb" * 32, 1, "faucet:" + "x" * 200))
            c.mine()
        with open(c.path, "rb") as f:
            return c.path, f.read()

    def test_chain_cut_anywhere_in_the_last_block_recovers(self):
        path, data = self.chain_file()
        start = data.rstrip(b"\n").rfind(b"\n") + 1  # where the last block's line begins
        cuts = sorted(set(range(start, len(data) + 1, 37)) | {start, start + 1, len(data) - 2, len(data) - 1, len(data)})
        for cut in cuts:
            with open(path, "wb") as f:
                f.write(data[:cut])
            c = ch.Chain(path)
            whole = cut >= len(data) - 1  # only the newline lost: the block is complete
            self.assertEqual(len(c.blocks), 4 if whole else 3, cut)
            c.add_tx(c.new_tx(ch.FAUCET, "dd" * 32, 1, "faucet:after"))
            c.mine()  # the next append after a crash...
            again = ch.Chain(path)  # ...survives the next reload
            self.assertEqual(len(again.blocks), len(c.blocks), cut)
            self.assertTrue(again.valid()[0], cut)

    def test_seller_log_cut_anywhere_never_yields_a_damaged_word(self):
        path = os.path.join(self.tmp.name, "seller.log")
        lines = [f"{CID} {i} {i:064x}\n" for i in range(1, 6)]
        data = "".join(lines).encode()
        start = len(data) - len(lines[-1])
        for cut in range(start, len(data) + 1):
            with open(path, "wb") as f:
                f.write(data[:cut])
            book = channels.SellerBook(path, SELLER)
            best = book.best(CID)
            self.assertIn(best["i"], (4, 5), cut)
            self.assertEqual(best["w"], f"{best['i']:064x}", cut)  # never a half word
            book._record(CID, 9, f"{9:064x}")
            book.close()
            self.assertEqual(channels.SellerBook(path, SELLER).best(CID)["i"], 9, cut)

    def test_a_kept_record_whose_newline_was_lost_is_kept(self):
        path = os.path.join(self.tmp.name, "seller.log")
        with open(path, "wb") as f:
            f.write(f"{CID} 7 {7:064x}".encode())
        self.assertEqual(channels.SellerBook(path, SELLER).best(CID)["i"], 7)

    def test_atomic_write_leftover_tmp_is_harmless(self):
        path, _ = self.chain_file(2)
        with open(path + ".ckpt.tmp", "wb") as f:
            f.write(b"{half a checkpoi")  # killed mid-checkpoint
        with open(path + ".ckpt", "wb") as f:
            f.write(b"\x00" * 64)  # a power cut left zeros: ignored, full replay
        self.assertEqual(len(ch.Chain(path).blocks), 2)

    def test_older_chain_formats_are_never_cut(self):
        """A format 1/2 chain is one JSON document whose last line is ']' or '}': torn-tail
        repair must leave it alone (it is archived or upgraded, never trimmed)."""
        path, _ = self.chain_file(2)
        blocks = [b.to_dict() for b in ch.Chain(path).blocks]
        for doc in (blocks, {"format": 2, "blocks": blocks}):
            text = json.dumps(doc, indent=1).replace("\n", "\r\n")  # as the old writer left it: no final newline
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            before = os.path.getsize(path)
            if isinstance(doc, list):
                with self.assertRaises(ValueError):  # format 1: refused, archived by open_or_archive
                    ch.Chain(path)
                self.assertEqual(os.path.getsize(path), before)
                c = ch.Chain.open_or_archive(path)
                aside = [n for n in os.listdir(self.tmp.name) if ".v1-" in n]
                self.assertEqual(len(aside), 1)
                self.assertEqual(os.path.getsize(os.path.join(self.tmp.name, aside[0])), before)
                os.remove(os.path.join(self.tmp.name, aside[0]))
            else:
                self.assertEqual(len(ch.Chain(path).blocks), 2)  # format 2: upgraded in place

    def test_policy_values(self):
        self.assertIn(durable.POLICY, ("safe", "strict", "off"))
        self.assertTrue(durable.must_sync(True) or durable.POLICY == "off")


class KilledWriter(unittest.TestCase):
    ROUNDS = 12

    def test_kill_mid_write_loses_nothing_acknowledged(self):
        rng = random.Random(1402)
        with tempfile.TemporaryDirectory() as d:
            acked_blocks = acked_tick = 0
            kills = 0
            for _ in range(self.ROUNDS):
                p = subprocess.Popen([sys.executable, os.path.join(HERE, "_crash_writer.py"), d],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                seen = []
                t = threading.Thread(target=lambda: seen.extend(p.stdout), daemon=True)
                t.start()
                time.sleep(rng.uniform(0.3, 0.8))
                p.kill()  # TerminateProcess: no cleanup, no flush
                p.wait()
                t.join(5)
                kills += 1
                for line in seen:
                    kind, n = line.split()
                    if kind == "block":
                        acked_blocks = max(acked_blocks, int(n))
                    else:
                        acked_tick = max(acked_tick, int(n))
                c = ch.Chain(os.path.join(d, "chain.json"))
                self.assertTrue(c.valid()[0])
                self.assertGreaterEqual(len(c.blocks), acked_blocks)
                best = channels.SellerBook(os.path.join(d, "seller.log"), SELLER).best(CID) or {"i": 0, "w": "0" * 64}
                self.assertGreaterEqual(best["i"], acked_tick)
                self.assertEqual(best["w"], f"{best['i']:064x}")
            self.assertEqual(kills, self.ROUNDS)
            self.assertGreater(acked_blocks, self.ROUNDS)  # the writer made real progress


if __name__ == "__main__":
    unittest.main()
