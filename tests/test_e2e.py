"""End to end over HTTP on scratch ports: the agent pays, attacks over the wire fail.
Never touches the live chain.json, wallets or ports 8402/8403."""
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import buyer_agent  # noqa: E402
import chain as ch  # noqa: E402
import explorer  # noqa: E402
import server  # noqa: E402
import wallets  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read().decode()


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls._wd = wallets.WALLET_DIR
        wallets.WALLET_DIR = os.path.join(cls.tmp.name, "wallets")
        cls.chain = ch.Chain.open_or_archive(os.path.join(cls.tmp.name, "chain.json"))
        cls.port, cls.eport = free_port(), free_port()
        cls.api = server.start(cls.chain, port=cls.port)
        cls.exp = explorer.start(cls.chain, port=cls.eport, prices=server.PRICES)
        cls._api_url = buyer_agent.API
        buyer_agent.API = f"http://127.0.0.1:{cls.port}"
        cls.base = buyer_agent.API

    @classmethod
    def tearDownClass(cls):
        cls.api.settler_stop.set()
        cls.api.shutdown()
        cls.exp.shutdown()
        cls.api.server_close()
        cls.exp.server_close()
        buyer_agent.API = cls._api_url
        wallets.WALLET_DIR = cls._wd
        cls.tmp.cleanup()

    def test_1_agent_pays_and_gets_data(self):
        w = wallets.load_or_create("e2e-buyer")
        buyer_agent.ensure_funded(self.chain, w, 50)
        for n in range(6):  # several paid calls: keys must rotate every time
            log = buyer_agent.buy_once(self.chain, w, "e2e", n, "/premium/price")
            self.assertTrue(log["ok"], log)
        log = buyer_agent.buy_once(self.chain, w, "e2e", 7, "/premium/history")
        self.assertTrue(log["ok"], log)
        self.assertEqual(self.chain.balance(w.address), 100 - 6 * (5 + 1) - (20 + 1))  # each tx: a 1 PMX fee
        self.assertTrue(self.chain.valid()[0])

    def test_2_replayed_tx_and_header_are_refused(self):
        tx = [t for t in self.chain.all_txs() if t["amount"] == 5][-1]
        code, _ = http(self.base + "/tx", json.dumps(tx).encode(), {"Content-Type": "application/json"})
        self.assertEqual(code, 400)
        code, _ = http(self.base + "/premium/price", headers={"X-Payment": tx["txid"]})
        self.assertEqual(code, 402)

    def test_3_bad_requests_get_400_not_a_crash(self):
        for body in (b"not json", b"[]", b'{"amount": -5}', json.dumps({"from_wallet": "0" * 64}).encode()):
            code, _ = http(self.base + "/tx", body, {"Content-Type": "application/json"})
            self.assertEqual(code, 400, body)
        try:  # the server refuses before reading the body; Windows may abort the upload
            code, _ = http(self.base + "/tx", b"x" * (server.MAX_TX_BYTES + 1), {"Content-Type": "application/json"})
        except (ConnectionError, urllib.error.URLError):
            code = 400
        self.assertEqual(code, 400)
        code, _ = http(self.base + "/faucet?name=../../evil")
        self.assertEqual(code, 400)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "evil.wallet.json")))
        code, _ = http(self.base + "/health")
        self.assertEqual(code, 200)  # still up

    def test_3b_nobody_mints_through_post_tx(self):
        me = "ab" * 32
        for amount in (10**12, 100):
            tx = ch.Chain.new_tx(ch.FAUCET, me, amount, "faucet:gift")
            code, _ = http(self.base + "/tx", json.dumps(tx).encode(), {"Content-Type": "application/json"})
            self.assertEqual(code, 400)
        code, body = http(self.base + f"/account?address={me}")
        self.assertEqual(json.loads(body)["balance"], 0)

    def test_5_spam_gets_429_with_retry_after(self):
        old = server.LIMITS.limits["faucet"]
        server.LIMITS.limits["faucet"] = (3, 0.01)
        try:
            codes = [http(self.base + f"/faucet?address={'cd' * 32}")[0] for _ in range(6)]
        finally:
            server.LIMITS.limits["faucet"] = old
            server.LIMITS.buckets.clear()
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(set(codes[3:]), {429})
        self.assertEqual(http(self.base + "/health")[0], 200)

    def test_4_explorer_escapes_hostile_memos(self):
        w = wallets.load_or_create("e2e-xss")
        buyer_agent.ensure_funded(self.chain, w, 10)
        live = ch.Chain(self.chain.path)
        evil = '</script><script>alert(1)</script><img src=x onerror=alert(2)>'
        tx = live.new_tx(w.address, "a" * 64, 1, evil)
        wallets.sign_tx(tx, w, live)
        code, _ = http(self.base + "/tx", json.dumps(tx).encode(), {"Content-Type": "application/json"})
        self.assertEqual(code, 200)
        code, page = http(f"http://127.0.0.1:{self.eport}/")
        self.assertEqual(code, 200)
        self.assertNotIn("</script><script>alert", page)
        self.assertNotIn("<img src=x", page)


if __name__ == "__main__":
    unittest.main()
