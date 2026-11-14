"""Multi-seller hub: one buyer deposit with the node's hub pays the node's own API and a
seller running apart; one channel_close pays both (splits). A word pays one seller only."""
import os
import sys
import tempfile
import threading
import unittest
from wsgiref.simple_server import make_server

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "examples"))

import chain as ch  # noqa: E402
import channels  # noqa: E402
import server  # noqa: E402
import wallets  # noqa: E402
import weather_api  # noqa: E402
from paymesh_client import Client  # noqa: E402
from test_attacks import Scratch  # noqa: E402
from test_e2e import free_port  # noqa: E402
from test_seller import Quiet  # noqa: E402


class Hub(unittest.TestCase):
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
        cls.shop = f"http://127.0.0.1:{cls.sport}"

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

    def test_one_deposit_pays_two_sellers_and_one_close_pays_both(self):
        c = Client(self.node, "hub-buyer")
        c.faucet()
        node_seller, shop = server.SELLER.address, self.paywall.wallet.address
        before = {a: self.chain.balance(a) for a in (node_seller, shop)}
        c.open_hub(ticks=60)
        height = self.chain.height
        for _ in range(2):
            self.assertEqual(c.get("/premium/price")[0], 200)  # the node's API: 5 PMX
        for _ in range(3):
            code, body = c.get(self.shop + "/forecast")  # a seller apart: 3 PMX
            self.assertEqual(code, 200, body)
        self.assertEqual(self.chain.height, height)  # 5 paid calls at 2 sellers, no tx
        res = c.close_channel()
        self.assertEqual(res["paid_to_seller"], 19)
        cid = [k for k, v in self.chain.ledger.channels.items() if v["buyer"] == c.wallet.address][-1]
        # the close's 1 PMX fee comes out of the largest share
        self.assertEqual(self.chain.ledger.channels[cid]["splits"], {node_seller: 9, shop: 9})
        self.assertEqual(self.chain.balance(node_seller) - before[node_seller], 9)
        self.assertEqual(self.chain.balance(shop) - before[shop], 9)
        self.assertEqual(c.balance(), 100 - 19 - 1)  # and the open's fee
        self.assertTrue(ch.Chain(self.chain.path).valid()[0])

    def test_a_word_pays_one_seller_only(self):
        c = Client(self.node, "hub-double")
        c.faucet()
        c.open_hub(ticks=30)
        c.get("/premium/price")  # learn offers
        c.get(self.shop + "/forecast")
        h = c.channel.pay(5)
        self.assertEqual(c._req("/premium/price", headers={"X-Channel": h})[0], 200)
        code, body = c._req(self.shop + "/forecast", headers={"X-Channel": h})  # the same word, elsewhere
        self.assertEqual(code, 402, body)
        self.assertIn("not after the last paid tick", body["reason"])

    def test_hub_claim_refuses_a_bad_payee_and_foreign_channels(self):
        c = Client(self.node, "hub-claims")
        c.faucet()
        c.open_hub(ticks=10)
        cid, i, w = c.channel.pay(1).split()
        for payee in (ch.FAUCET, "zz", 5):
            code, _ = c._node("/hub/claim", {"channel": cid, "tick": int(i), "word": w, "seller": payee, "price": 1})
            self.assertIn(code, (400, 402), payee)
        d = Client(self.node, "hub-direct")
        d.faucet()
        d.open_channel(ticks=10)  # a channel to the node's own seller, not to the hub
        did, di, dw = d.channel.pay(1).split()
        code, body = d._node("/hub/claim", {"channel": did, "tick": int(di), "word": dw,
                                            "seller": self.paywall.wallet.address, "price": 1})
        self.assertEqual(code, 402, body)


class HubBook(Scratch):
    def open_hub(self, ticks=100):
        self.buyer = self.funded("buyer", 500)
        self.hub = self.funded("hub", 0)
        self.bc = channels.BuyerChannel(ticks)
        self.c.add_tx(self.bc.open_tx(self.c, self.buyer, self.hub.address, 1, 200))
        self.c.mine()
        return channels.SellerBook(os.path.join(self.tmp.name, "hub.log"), self.hub.address)

    def test_credits_survive_a_restart_and_compaction(self):
        book = self.open_hub()
        a, b = "a1" * 32, "b2" * 32
        old = channels.SellerBook.COMPACT_AT
        channels.SellerBook.COMPACT_AT = 7
        try:
            for n in range(20):
                ok, why = book.accept(self.c, self.bc.pay(2), 2, payee=a if n % 3 else b)
                self.assertTrue(ok, why)
        finally:
            channels.SellerBook.COMPACT_AT = old
        book.close()
        again = channels.SellerBook(book.path, self.hub.address)
        self.assertEqual(again.credits[self.bc.id], {a: 26, b: 14})
        self.assertEqual(again.best(self.bc.id)["i"], 40)
        tx = again.close_tx(self.c, self.hub, self.bc.id)
        self.c.add_tx(tx)
        self.c.mine()
        self.assertEqual((self.c.balance(a), self.c.balance(b), self.c.balance(self.buyer.address)), (25, 14, 459))

    def test_splits_rules(self):
        book = self.open_hub()
        book.accept(self.c, self.bc.pay(4), 4, payee="a1" * 32)
        good = book.close_tx(self.c, self.hub, self.bc.id)
        bad = []
        for splits in ({"a1" * 32: 5}, {"a1" * 32: 2, "b2" * 32: 3}, {ch.FAUCET: 4}, {"a1" * 32: 4.0},
                       {"a1" * 32: 0, "b2" * 32: 4}, {}, {f"{n:064x}": 1 for n in range(1, 66)}):
            t = self.c.new_tx(self.hub.address, self.buyer.address, 4, "channel:close")
            t.update(kind="channel_close", channel=self.bc.id, ticks=4, word=good["word"], splits=splits)
            bad.append(t)
        for t in bad:
            wallets.sign_tx(t, self.hub, self.c)
            with self.assertRaises(ValueError, msg=t["splits"] if len(t["splits"]) < 5 else "65 payees"):
                self.c.add_tx(t)
        stolen = dict(good, splits={"c3" * 32: 4})  # rewrite who gets paid after the hub signed
        with self.assertRaises(ValueError):
            self.c.add_tx(stolen)
        self.c.add_tx(book.close_tx(self.c, self.hub, self.bc.id))
        self.c.mine()
        self.assertEqual(self.c.balance("a1" * 32), 3)  # 4, the fee out of it

    def test_a_hub_channel_word_is_not_taken_without_a_payee(self):
        book = self.open_hub()
        book.accept(self.c, self.bc.pay(1), 1, payee="a1" * 32)
        ok, why = book.accept(self.c, self.bc.pay(1), 1)  # straight to the hub's own book
        self.assertFalse(ok)
        self.assertIn("only through the hub", why)


if __name__ == "__main__":
    unittest.main()
