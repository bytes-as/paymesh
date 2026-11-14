"""QA findings: each test reproduces one bug found by QA. A known open bug is marked
expectedFailure so the suite stays green; when the fix lands the test reports an
"unexpected success": then drop the decorator. QA-PM-1 and QA-PM-2 are fixed. Scratch node only (temp dir, free port)."""
import http.client
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chain as ch  # noqa: E402
import channels  # noqa: E402
import server  # noqa: E402
import wallets  # noqa: E402
from paymesh_client import Client  # noqa: E402
from test_e2e import free_port  # noqa: E402


class QAFindings(unittest.TestCase):
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

    def _get(self, path, headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path, headers=headers)
            r = conn.getresponse()
            r.read()
            return r.status
        except (http.client.RemoteDisconnected, ConnectionError) as e:
            return f"no response ({type(e).__name__})"
        finally:
            conn.close()

    # QA-PM-1: str.isdigit() accepts '²' (latin-1 0xB2 in a header) but int() refuses it:
    # SellerBook.accept raises ValueError, the request thread dies, client gets no response.
    def test_qa_pm_1_superscript_tick_gets_402_not_dropped(self):
        for h in ("ab \xb2 " + "ab" * 32, "ab 1\xb9 " + "ab" * 32, "ab \xb3 " + "ab" * 32):
            self.assertEqual(self._get("/premium/price", {"X-Channel": h}), 402, repr(h))

    # QA-PM-2: SellerBook.accept never looks at the channel's expiry. A buyer opens a channel
    # that is refundable at once (expires = height + 1), spends every tick on paid calls,
    # then refunds the whole deposit before the 2 s settler runs: service for free.
    def test_qa_pm_2_expired_channel_cannot_buy_then_refund(self):
        cl = Client(f"http://127.0.0.1:{self.port}", "qa-buyer")
        cl.faucet()
        seller = cl._node("/channel/terms")[1]["seller"]
        bc = channels.BuyerChannel(50)
        led = cl._ledger()
        tx = led.new_tx(cl.wallet.address, seller, 50, "channel:open")
        tx.update(kind="channel_open", anchor=bc.anchor, ticks=50, unit=1, expires=self.chain.height + 1)
        wallets.sign_tx(tx, cl.wallet, led)
        self.assertEqual(cl._req("/tx", tx)[0], 200)
        bc.id = tx["txid"]
        served = sum(self._get("/premium/price", {"X-Channel": bc.pay(5)}) == 200 for _ in range(10))
        led = cl._ledger()
        rtx = led.new_tx(cl.wallet.address, cl.wallet.address, 50, "channel:refund")
        rtx.update(kind="channel_refund", channel=bc.id)
        wallets.sign_tx(rtx, cl.wallet, led)
        cl._req("/tx", rtx)
        paid = self.chain.ledger.channels[bc.id].get("paid", 0)
        # every served call must end up paid to the seller: served * 5 PMX
        self.assertEqual(paid, served * 5, f"served {served} paid calls, seller was paid {paid} PMX")

    # QA-PM-2b: the cutoff alone races: pay while the channel is just outside it, then mine
    # blocks fast (each tx is a block) and refund before the 2 s settler wakes. The node is
    # the seller and the miner, so it settles first when that refund arrives.
    def test_qa_pm_2b_refund_race_is_settled_first(self):
        cl = Client(f"http://127.0.0.1:{self.port}", "qa-racer")
        cl.faucet()
        seller = cl._node("/channel/terms")[1]["seller"]
        bc = channels.BuyerChannel(50)
        led = cl._ledger()
        tx = led.new_tx(cl.wallet.address, seller, 50, "channel:open")
        expires = self.chain.height + channels.SellerBook.STOP_WITHIN + 3
        tx.update(kind="channel_open", anchor=bc.anchor, ticks=50, unit=1, expires=expires)
        wallets.sign_tx(tx, cl.wallet, led)
        self.assertEqual(cl._req("/tx", tx)[0], 200)
        bc.id = tx["txid"]
        served = sum(self._get("/premium/price", {"X-Channel": bc.pay(5)}) == 200 for _ in range(4))
        self.assertGreater(served, 0)
        while self.chain.height < expires:
            cl._node(f"/faucet?address={'ee' * 32}")  # one block each
        led = cl._ledger()
        rtx = led.new_tx(cl.wallet.address, cl.wallet.address, 50, "channel:refund")
        rtx.update(kind="channel_refund", channel=bc.id)
        wallets.sign_tx(rtx, cl.wallet, led)
        cl._req("/tx", rtx)
        self.assertEqual(self.chain.ledger.channels[bc.id].get("paid", 0), served * 5)


if __name__ == "__main__":
    unittest.main()
