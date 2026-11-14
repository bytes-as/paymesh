"""A separate seller API (examples/weather_api.py) charging through paymesh_seller:
on-chain, channel and settlement all go through the node over HTTP."""
import os
import sys
import tempfile
import threading
import unittest
from wsgiref.simple_server import WSGIRequestHandler, make_server

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "examples"))

import chain as ch  # noqa: E402
import server  # noqa: E402
import wallets  # noqa: E402
import weather_api  # noqa: E402
from paymesh_client import Client, PaymentError  # noqa: E402
from test_e2e import free_port  # noqa: E402


class Quiet(WSGIRequestHandler):
    def log_message(self, *a):
        pass


class SellerApart(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls._wd = wallets.WALLET_DIR
        wallets.WALLET_DIR = os.path.join(cls.tmp.name, "wallets")
        cls.chain = ch.Chain.open_or_archive(os.path.join(cls.tmp.name, "chain.json"))
        cls.nport, cls.sport = free_port(), free_port()
        cls.node_srv = server.start(cls.chain, port=cls.nport)
        cls.node = f"http://127.0.0.1:{cls.nport}"
        cls.paywall, app = weather_api.build(cls.node)
        cls.httpd = make_server("127.0.0.1", cls.sport, app, server_class=weather_api.ThreadingWSGIServer, handler_class=Quiet)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.api = f"http://127.0.0.1:{cls.sport}"

    @classmethod
    def tearDownClass(cls):
        cls.paywall.stop.set()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.node_srv.settler_stop.set()
        cls.node_srv.shutdown()
        cls.node_srv.server_close()
        wallets.WALLET_DIR = cls._wd
        cls.tmp.cleanup()

    def test_pay_on_chain_then_by_channel_then_settle(self):
        c = Client(self.api, "weather-buyer", node=self.node)
        c.faucet()
        self.assertEqual(c.get("/")[0], 200)  # free
        code, body = c.get("/forecast")  # 402 from the seller, tx to the node, retry at the seller
        self.assertEqual(code, 200, body)
        self.assertIn("tomorrow_c", body)
        receipt = c.last_receipt()  # PAYMENT-RESPONSE from the seller, which settled through the node
        self.assertTrue(receipt["success"], receipt)
        self.assertEqual(self.chain.find_tx_by_id(receipt["transaction"])[0]["from_wallet"], c.wallet.address)
        seller = self.paywall.wallet.address
        self.assertEqual(self.chain.balance(seller), 3)
        c.open_channel(ticks=30)
        for _ in range(10):
            code, body = c.get("/forecast")
            self.assertEqual(code, 200, body)
        res = c.close_channel()
        self.assertEqual(res["paid_to_seller"], 30)
        self.assertEqual(self.chain.balance(seller), 3 + 30 - 1)  # the close's fee out of the seller's 30
        self.assertEqual(c.balance(), 100 - (3 + 1) - (30 + 1))  # the pay tx's and the open's fees
        self.assertTrue(self.chain.valid()[0])

    def test_replays_and_fakes_fail_at_the_seller(self):
        c = Client(self.api, "weather-cheat", node=self.node)
        c.faucet()
        self.assertEqual(c.get("/forecast")[0], 200)
        txid = [t for t in self.chain.all_txs() if t["from_wallet"] == c.wallet.address][-1]["txid"]
        self.assertEqual(c._req("/forecast", headers={"X-Payment": txid})[0], 402)  # replay
        self.assertEqual(c._req("/forecast", headers={"X-Payment": "ab" * 32})[0], 402)  # made up
        c.open_channel(ticks=6)
        h = c.channel.pay(3)
        self.assertEqual(c._req("/forecast", headers={"X-Channel": h})[0], 200)
        self.assertEqual(c._req("/forecast", headers={"X-Channel": h})[0], 402)  # spent word
        with self.assertRaises(PaymentError):
            for _ in range(3):
                c.get("/forecast")


if __name__ == "__main__":
    unittest.main()
