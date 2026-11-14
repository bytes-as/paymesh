"""The client SDK against a real server on scratch ports: on-chain and channel payments."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chain as ch  # noqa: E402
import server  # noqa: E402
import wallets  # noqa: E402
from paymesh_client import Client, PaymentError  # noqa: E402
from test_e2e import free_port  # noqa: E402


class ClientSDK(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls._wd = wallets.WALLET_DIR
        wallets.WALLET_DIR = os.path.join(cls.tmp.name, "wallets")
        cls.chain = ch.Chain.open_or_archive(os.path.join(cls.tmp.name, "chain.json"))
        cls.port = free_port()
        cls.api = server.start(cls.chain, port=cls.port)
        cls.url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.api.settler_stop.set()
        cls.api.shutdown()
        cls.api.server_close()
        wallets.WALLET_DIR = cls._wd
        cls.tmp.cleanup()

    def test_on_chain_then_channel(self):
        c = Client(self.url, "sdk-agent")
        c.faucet()
        self.assertEqual(c.balance(), 100)
        code, body = c.get("/premium/price")  # no channel: one on-chain tx
        self.assertEqual(code, 200, body)
        self.assertEqual(c.balance(), 95)
        self.assertEqual(c.get("/open/info")[0], 200)  # free route, nothing paid
        c.open_channel(ticks=80)
        self.assertEqual(c.balance(), 15)  # 80 locked
        for _ in range(10):
            code, body = c.get("/premium/price")  # 5 ticks each, no txs
            self.assertEqual(code, 200, body)
        code, body = c.get("/premium/history")  # 20 ticks
        self.assertEqual(code, 200, body)
        blocks_before = self.chain.height
        with self.assertRaises(PaymentError):  # 70 of 80 ticks spent: 2 more calls fit, the 3rd can't pay
            for _ in range(5):
                c.get("/premium/price")
        res = c.close_channel()
        self.assertEqual(res["paid_to_seller"], 80)
        self.assertEqual(self.chain.height, blocks_before + 1)  # the whole channel cost 2 blocks
        self.assertEqual(c.balance(), 15)

    def test_channel_resumes_after_restart(self):
        c = Client(self.url, "sdk-resume")
        c.faucet()
        c.open_channel(ticks=40)
        self.assertEqual(c.get("/premium/price")[0], 200)
        again = Client(self.url, "sdk-resume")  # a new process with the same wallet
        self.assertIsNotNone(again.channel)
        self.assertEqual(again.get("/premium/price")[0], 200)  # first call learns the price via 402
        self.assertEqual(again.get("/premium/price")[0], 200)
        res = again.close_channel()
        self.assertEqual(res["paid_to_seller"], 15)


if __name__ == "__main__":
    unittest.main()
