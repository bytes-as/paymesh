"""A node killed in the middle of a hub payout neither pays a tick twice nor loses one.

A real node process on a scratch folder pays two sellers from a buyer's hub channel on a
short schedule. Each round the sellers claim more words, then the node dies during the
payout: at each injected point (signed / pending / mined, PAYMESH_CRASH_AT) and at random
moments (TerminateProcess). After every restart the claimed ticks are paid out exactly once;
at the end the channel closes and the chain file is replayed: sellers + fees + the buyer's
rest add up to the deposit, and each seller got its credits minus its share of the fees."""
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import chain as ch  # noqa: E402
import wallets  # noqa: E402
from paymesh_client import Client  # noqa: E402

A, B = "a1" * 32, "b2" * 32


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PayoutCrash(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "node")
        os.makedirs(self.home)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = dict(os.environ, PAYMESH_HOME=self.home, PAYMESH_API_PORT=str(self.port),
                        PAYMESH_EXPLORER_PORT=str(free_port()), PAYMESH_HUB_PAYOUT_S="1.0",
                        PAYMESH_HUB_PAYOUT_MIN="1000000")
        self.env.pop("PAYMESH_CRASH_AT", None)
        self.proc = None
        self._wd = wallets.WALLET_DIR
        wallets.WALLET_DIR = os.path.join(self.tmp.name, "buyer-wallets")

    def tearDown(self):
        self.stop()
        wallets.WALLET_DIR = self._wd
        self.tmp.cleanup()

    def start(self, crash_at=None):
        env = dict(self.env, **({"PAYMESH_CRASH_AT": crash_at} if crash_at else {}))
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "paymesh.py"), "serve"], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(200):
            try:
                urllib.request.urlopen(self.url + "/health", timeout=1).close()
                return
            except OSError:
                time.sleep(0.05)
        self.fail("node did not start")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()  # TerminateProcess: no cleanup
        if self.proc:
            self.proc.wait()

    def get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=10) as r:
            return json.load(r)

    def post(self, path, obj):
        req = urllib.request.Request(self.url + path, json.dumps(obj).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def paid_ticks(self):
        return self.get(f"/channel/info?id={self.cid}").get("paid_ticks", 0)

    def wait(self, cond, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if cond():
                    return True
            except OSError:
                pass
            time.sleep(0.05)
        return False

    def claim(self, n):
        """n words of 1 tick each, alternately to A and B (all before the payout timer fires)."""
        for _ in range(n):
            cid, i, w = self.buyer.channel.pay(1).split()
            payee = A if int(i) % 3 else B
            self.post("/hub/claim", {"channel": cid, "tick": int(i), "word": w, "seller": payee, "price": 1})
            self.credits[payee] += 1
            self.claimed += 1

    def test_crash_mid_payout_neither_double_pays_nor_loses(self):
        self.start()
        self.buyer = Client(self.url, "crash-buyer")
        self.buyer.faucet()
        self.buyer.faucet()
        self.buyer.open_hub(ticks=150)
        self.cid = self.buyer.channel.id
        self.buyer.close()
        self.credits, self.claimed, crashes = {A: 0, B: 0}, 0, []
        rounds = ["payout_signed", "payout_pending", "payout_mined", "random", "random"]
        rng = random.Random(9)
        for kind in rounds:
            self.stop()
            self.start(None if kind == "random" else kind)
            self.claim(rng.randint(5, 12))
            if kind == "random":
                time.sleep(rng.uniform(0.6, 1.6))  # around the payout timer
                self.proc.kill()
                code = self.proc.wait()
            else:
                code = self.proc.wait(timeout=15)  # the injected crash
                self.assertEqual(code, 70, kind)
            crashes.append((kind, code))
            self.start()  # restarted cleanly: the payout is rebuilt from the chain, once
            self.assertTrue(self.wait(lambda: self.paid_ticks() == self.claimed), f"after {kind}: not paid out")
            time.sleep(1.5)  # another timer round: nothing more must be paid
            self.assertEqual(self.paid_ticks(), self.claimed, kind)
            bal = {p: self.get(f"/account?address={p}")["balance"] for p in (A, B)}
            self.assertLessEqual(bal[A] + bal[B], self.claimed, f"after {kind}: paid more than claimed")
        self.post("/channel/close", {"channel": self.cid})
        self.stop()

        c = ch.Chain(os.path.join(self.home, "chain.json"))  # replays and checks every block
        self.assertTrue(c.valid()[0])
        got, fees, back, payouts = {A: 0, B: 0}, 0, 0, 0
        for blk in c.blocks:
            for tx in blk.txs:
                if tx.get("channel") != self.cid:
                    continue
                fees += tx.get("fee", 0)
                for who, part in (tx.get("splits") or {}).items():
                    got[who] += part
                if tx["kind"] == "channel_payout":
                    payouts += 1
                if tx["kind"] == "channel_close":
                    # nothing new at the close (all paid out): its fee comes out of the buyer's rest
                    back = 150 - tx["amount"] - (0 if "splits" in tx else tx.get("fee", 0))
        deposit = 150
        self.assertEqual(c.ledger.channels[self.cid]["paid_ticks"], self.claimed)
        self.assertGreaterEqual(payouts, len(rounds) - 1)  # a crash before mining leaves no payout behind
        self.assertEqual(got[A] + got[B] + fees + back, deposit, "sellers + fees + the buyer's rest = the deposit")
        for p in (A, B):  # each seller: its credits, less at most every fee
            self.assertLessEqual(got[p], self.credits[p])
            self.assertGreaterEqual(got[p], self.credits[p] - fees)
        self.assertEqual(c.balance(A) + c.balance(B), got[A] + got[B])


if __name__ == "__main__":
    unittest.main()
