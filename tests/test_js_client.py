"""js/paymesh.js against the Python implementation, byte for byte: SHA-256, Python-style JSON,
WOTS v2 keys and whole signed txs (cross-test vectors), then the JS client paying a scratch
node end to end. Runs the JS with Node.js; skipped when `node` is not installed."""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import chain as ch  # noqa: E402
import paymesh_x402 as x402  # noqa: E402
import server  # noqa: E402
import wallets  # noqa: E402
from test_e2e import free_port  # noqa: E402

NODE = shutil.which("node")
JS = os.path.join(ROOT, "js", "paymesh.js")
SEED = bytes(range(32))


def js(body: str, data=None, timeout=120):
    """Run `body` (async JS with P = the library, input = data) under Node; return its JSON output."""
    prog = ("const P = require(process.argv[1]);\n"
            "let raw = ''; process.stdin.on('data', d => raw += d).on('end', async () => {\n"
            "  const input = JSON.parse(raw); const out = await (async () => {" + body + "})();\n"
            "  process.stdout.write(JSON.stringify(out)); });\n")
    r = subprocess.run([NODE, "-e", prog, JS], input=json.dumps(data), capture_output=True, text=True,
                       encoding="utf-8", timeout=timeout)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)


class FixedLedger:
    def __init__(self, key):
        self.key = key

    def current_key(self, address):
        return self.key


@unittest.skipUnless(NODE, "Node.js is not installed")
class Vectors(unittest.TestCase):
    def setUp(self):
        self._wd = wallets.WALLET_DIR
        self.tmp = tempfile.TemporaryDirectory()
        wallets.WALLET_DIR = self.tmp.name  # sign_tx saves wallets that have a file: these have none

    def tearDown(self):
        wallets.WALLET_DIR = self._wd
        self.tmp.cleanup()

    def test_sha256(self):
        rng = random.Random(8)
        inputs = [b"", b"abc"] + [bytes(rng.randrange(256) for _ in range(n)) for n in (55, 56, 63, 64, 65, 1000, 100_000)]
        out = js("return input.map(h => P.sha256hex(P.unhex(h)));", [x.hex() for x in inputs])
        self.assertEqual(out, [wallets.sha256_hex(x) for x in inputs])

    def test_python_json(self):
        objs = [{"b": 1, "a": [1, -2, 2**53 - 1], "c": None, "d": True, "e": False, "f": {}, "g": []},
                {"memo": "".join(chr(c) for c in range(0x20)) + '\x7f "quote" \\ / é 日本 😀 \ud800 |'},
                {"splits": [["aa" * 32, 3], ["bb" * 32, 4]], "Z": "x", "a": "y", "fee": 1},
                "plain", 0, []]
        out = js("return input.map(o => [P.pyJson(o, false), P.pyJson(o, true)]);", objs)
        self.assertEqual(out, [[json.dumps(o, sort_keys=True), json.dumps(o, sort_keys=True, separators=(",", ":"))]
                               for o in objs])

    def test_wots2_keys(self):
        w = wallets.Wallet("vec", SEED)
        idx = [0, 1, 7, 300]
        out = js("const w = new P.Wallet(P.unhex(input.seed));"
                 "return input.idx.map(i => [P.hex(w.key(i).pkSeed), w.keyFingerprint(i)]);", {"seed": SEED.hex(), "idx": idx})
        self.assertEqual(out, [[w.key(i)[2].hex(), w.key_fp(i)] for i in idx])

    def txs(self, w):
        to = "cd" * 32
        return [
            (0, {"from_wallet": w.address, "to_wallet": to, "amount": 5, "memo": "x402:/premium/price:5:" + "ab" * 16,
                 "txid": "11" * 32}),
            (3, {"from_wallet": w.address, "to_wallet": to, "amount": 7, "memo": 'café ☕ 日本 😀 | "q" \\ \n',
                 "txid": "22" * 32}),
            (9, {"from_wallet": w.address, "to_wallet": to, "amount": 30, "memo": "channel:open", "txid": "33" * 32,
                 "kind": "channel_open", "anchor": "44" * 32, "ticks": 30, "unit": 1, "expires": 10_012}),
            (12, {"from_wallet": w.address, "to_wallet": to, "amount": 9, "memo": "channel:close", "txid": "55" * 32,
                  "kind": "channel_close", "channel": "66" * 32, "ticks": 9, "word": "77" * 32,
                  "splits": [["88" * 32, 5], ["99" * 32, 3]], "fee": 1}),
            (40, {"from_wallet": w.address, "to_wallet": to, "amount": 1, "memo": "m" * 9000, "txid": "aa" * 32}),  # fee 2
            (41, {"from_wallet": w.address, "to_wallet": w.address, "amount": 8, "memo": "channel:payout", "txid": "bb" * 32,
                  "kind": "channel_payout", "channel": "66" * 32, "ticks": 20, "word": "77" * 32,
                  "shares": {"88" * 32: 5, "99" * 32: 3}, "splits": {"88" * 32: 4, "99" * 32: 3}}),
        ]

    def test_signed_txs_match_python_byte_for_byte(self):
        w = wallets.Wallet("vec", SEED)
        cases = self.txs(w)
        py = []
        for i, tx in cases:
            signed = wallets.sign_tx(json.loads(json.dumps(tx)), wallets.Wallet("vec", SEED, i), FixedLedger(w.key_fp(i)))
            py.append(json.dumps(signed, sort_keys=True, separators=(",", ":")))
        out = js("const w = new P.Wallet(P.unhex(input.seed));"
                 "return input.cases.map(([i, tx]) => P.pyJson(P.signTx(tx, w, i), true));",
                 {"seed": SEED.hex(), "cases": cases})
        self.assertEqual(out, py)
        self.assertEqual([json.loads(s)["fee"] for s in out], [1, 1, 1, 1, 2, 1])
        for (i, _), s in zip(cases, out):  # and Python's verifier accepts the JS signatures
            self.assertEqual(wallets.verify_tx(json.loads(s), w.key_fp(i)), (True, "ok"))
            bad = json.loads(s)
            bad["amount"] += 1
            self.assertFalse(wallets.verify_tx(bad, w.key_fp(i))[0])

    def test_python_wallet_file_and_x402_encoding(self):
        w = wallets.Wallet("vec", SEED, next_index=4)
        out = js("const w = P.Wallet.fromJSON(input.w); const e = P.encode(input.obj);"
                 "return [w.address, w.nextIndex, e, P.decode(input.py)];",
                 {"w": w.to_dict(), "obj": {"x402Version": 2, "memo": "é😀"}, "py": x402.encode({"a": "é😀", "n": 1})})
        self.assertEqual(out[:2], [w.address, 4])
        self.assertEqual(x402.decode(out[2]), {"x402Version": 2, "memo": "é😀"})
        self.assertEqual(out[3], {"a": "é😀", "n": 1})
        for bad in (dict(w.to_dict(), wots2_from=None), dict(w.to_dict(), address="00" * 32)):
            with self.assertRaises(AssertionError):
                js("P.Wallet.fromJSON(input); return 1;", bad)


@unittest.skipUnless(NODE, "Node.js is not installed")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls._wd = wallets.WALLET_DIR
        wallets.WALLET_DIR = os.path.join(cls.tmp.name, "wallets")
        cls.chain = ch.Chain.open_or_archive(os.path.join(cls.tmp.name, "chain.json"))
        cls.port = free_port()
        cls.api = server.start(cls.chain, port=cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.api.settler_stop.set()
        cls.api.shutdown()
        cls.api.server_close()
        wallets.WALLET_DIR = cls._wd
        cls.tmp.cleanup()

    def test_the_js_client_pays_a_route_twice(self):
        seed = os.urandom(32)
        addr = wallets.Wallet("jsbuyer", seed).address
        url = f"http://127.0.0.1:{self.port}"
        import urllib.request
        urllib.request.urlopen(f"{url}/faucet?address={addr}").read()
        out = js("const w = new P.Wallet(P.unhex(input.seed)); const c = new P.Client(input.url, w);"
                 "const a = await c.get('/premium/price'); const b = await c.get('/premium/price');"
                 "return [a.status, a.receipt, b.status, b.receipt.success, w.nextIndex, w.locked, w.address];",
                 {"seed": seed.hex(), "url": url})
        self.assertEqual(out[0], 200)
        self.assertTrue(out[1]["success"])
        self.assertEqual(out[2:], [200, True, 2, None, addr])
        self.assertEqual(self.chain.balance(addr), 100 - 2 * (5 + 1))
        self.assertIsNotNone(self.chain.find_tx_by_id(out[1]["transaction"])[0])

    def test_a_refused_payment_locks_the_exposed_key(self):
        seed = os.urandom(32)  # never funded: the payment is refused after the seller saw the signature
        out = js("const w = new P.Wallet(P.unhex(input.seed)); const c = new P.Client(input.url, w);"
                 "const a = await c.get('/premium/price'); let again;"
                 "try { await c.get('/premium/price'); again = 'paid'; } catch (e) { again = e.message; }"
                 "return [a.status, a.receipt && a.receipt.success, w.locked, w.nextIndex, again];",
                 {"seed": seed.hex(), "url": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(out[:4], [402, False, 0, 0])
        self.assertIn("must not sign again", out[4])


if __name__ == "__main__":
    unittest.main()
