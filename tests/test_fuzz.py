"""Fuzzing the HTTP surface: mutated txs and payment headers never crash the server
or slip through. Seeded, so a failure reproduces."""
import json
import os
import random
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chain as ch  # noqa: E402
import server  # noqa: E402
import wallets  # noqa: E402
from paymesh_client import Client  # noqa: E402
from test_e2e import free_port  # noqa: E402

WEIRD = [None, True, -1, 0, 2**70, 1.5, "", "x" * 300, "0" * 64, "ff" * 32, [], {}, ["a"], {"a": 1}, "‮\u0000", "<script>"]


def mutate(rng, tx):
    while True:  # a mutation that changes nothing is just the valid tx: draw again
        t = _mutate(rng, tx)
        if t != tx:
            return t


def _mutate(rng, tx):
    t = json.loads(json.dumps(tx))
    for _ in range(rng.randint(1, 3)):
        k = rng.choice(list(t.keys()) + ["kind", "channel", "anchor", "ticks", "word", "extra"])
        op = rng.random()
        if op < 0.25:
            t.pop(k, None)
        elif op < 0.6:
            t[k] = rng.choice(WEIRD)
        elif op < 0.8 and isinstance(t.get(k), list) and t[k]:
            t[k] = t[k][: rng.randint(0, len(t[k]) - 1)]
        else:
            t["kind"] = rng.choice(["pay", "channel_open", "channel_close", "channel_refund", "mint", 7])
    return t


class Fuzz(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls._wd = wallets.WALLET_DIR
        wallets.WALLET_DIR = os.path.join(cls.tmp.name, "wallets")
        cls.chain = ch.Chain.open_or_archive(os.path.join(cls.tmp.name, "chain.json"))
        cls.port = free_port()
        cls.api = server.start(cls.chain, port=cls.port)
        cls.c = Client(f"http://127.0.0.1:{cls.port}", "fuzz")

    @classmethod
    def tearDownClass(cls):
        cls.api.settler_stop.set()
        cls.api.shutdown()
        cls.api.server_close()
        wallets.WALLET_DIR = cls._wd
        cls.tmp.cleanup()

    def test_mutated_txs_are_refused_never_crash(self):
        rng = random.Random(402)
        self.c.faucet()
        led = self.c._ledger()
        base = led.new_tx(self.c.wallet.address, "ab" * 32, 3, "fuzz")
        wallets.sign_tx(base, self.c.wallet, led)
        balance = self.c.balance()
        codes = {}
        for _ in range(1000):
            code, _ = self.c._req("/tx", mutate(rng, base))
            codes[code] = codes.get(code, 0) + 1
        self.assertEqual(set(codes), {400}, codes)
        for body in (b"\xff\xfe", b"{", b"null", b"1e999", b'"x"', b"[" * 5000):
            code, _ = self._raw("/tx", body)
            self.assertEqual(code, 400, body[:20])
        self.assertEqual(self.c.balance(), balance)  # nothing moved
        self.assertEqual(self.c._req("/health")[0], 200)
        self.assertTrue(self.chain.valid()[0])

    def test_garbage_payment_headers_are_refused(self):
        rng = random.Random(7)
        self.c.faucet()
        self.c.open_channel(ticks=20)
        cid = self.c.channel.id
        heads = ["", " ", "x", f"{cid}", f"{cid} 1", f"{cid} -1 {'ab' * 32}", f"{cid} 99999999999999999999 {'ab' * 32}",
                 f"{cid} 1 {'zz' * 32}", f"{cid} 1 {'ab' * 32} extra", "\u0000 1 2"]
        heads += [" ".join(rng.choice([cid, str(rng.randint(-5, 50)), "ab" * 32, "q"]) for _ in range(rng.randint(1, 4))) for _ in range(300)]
        for h in heads:
            code, _ = self.c._req("/premium/price", headers={"X-Channel": h})
            self.assertEqual(code, 402, h)
        for h in ["", "zz", "ab" * 32, "' OR 1=1", "x" * 5000]:
            code, _ = self.c._req("/premium/price", headers={"X-Payment": h})
            self.assertEqual(code, 402, h[:20])
        self.assertEqual(self.c._req("/health")[0], 200)

    def test_mutated_x402_payment_signatures_are_refused(self):
        import base64
        import paymesh_x402 as x402
        rng = random.Random(4022)
        self.c.faucet()
        code, _ = self.c._req("/premium/price")
        offers = self.c._offers("/premium/price")
        led = self.c._ledger()
        tx = led.new_tx(self.c.wallet.address, offers["tx"]["payTo"], 5, offers["tx"]["extra"]["memo"])
        wallets.sign_tx(tx, self.c.wallet, led)
        good = [{"x402Version": 2, "accepted": offers["tx"], "payload": {"transaction": tx}},
                {"x402Version": 2, "accepted": offers["channel"], "payload": {"channel": "ab" * 32, "tick": 1, "word": "cd" * 32}}]
        junk = [None, -1, 0, 1.5, True, "", "x" * 300, [], {}, "ab" * 32, 10**30]
        balance, codes = self.c.balance(), {}

        def poke(obj):
            obj = json.loads(json.dumps(obj))
            for _ in range(rng.randint(1, 3)):
                node, keys = obj, []
                while isinstance(node, dict) and node and rng.random() < 0.7:
                    k = rng.choice(list(node))
                    keys.append(k)
                    if not isinstance(node[k], dict):
                        break
                    node = node[k]
                tgt = obj
                for k in keys[:-1]:
                    tgt = tgt[k]
                if keys:
                    if rng.random() < 0.2:
                        del tgt[keys[-1]]
                    else:
                        tgt[keys[-1]] = rng.choice(junk)
                else:
                    obj["extra_" + str(rng.randint(0, 9))] = rng.choice(junk)
            return obj

        heads = [x402.encode(poke(rng.choice(good))) for _ in range(400)]
        heads += [base64.b64encode(rng.randbytes(rng.randint(0, 200))).decode() for _ in range(50)]
        heads += ["", "=", "e30=", "W10=", "bnVsbA=="]  # "{}", "[]", "null"
        for h in heads:
            code, _ = self.c._req("/premium/price", headers={x402.SIGNATURE: h})
            codes[code] = codes.get(code, 0) + 1
        # never a crash; the one signed tx can pay at most once (a mutation of a field the
        # payment doesn't depend on, e.g. maxTimeoutSeconds, still carries a valid tx)
        self.assertLessEqual(set(codes), {200, 400, 402}, codes)
        self.assertLessEqual(codes.get(200, 0), 1, codes)
        self.assertEqual(self.c.balance(), balance - 5 * codes.get(200, 0))
        self.assertEqual(self.c._req("/health")[0], 200)
        self.assertTrue(self.chain.valid()[0])

    def _raw(self, path, body):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            r.read()
            return r.status, None
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
