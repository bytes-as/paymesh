"""x402 facilitator endpoints (POST /verify, POST /settle, GET /supported) for the paymesh
scheme: a resource server that runs no paymesh code delegates its payments to the node."""
import http.client
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chain as ch  # noqa: E402
import facilitator  # noqa: E402
import paymesh_x402 as x402  # noqa: E402
import server  # noqa: E402
import wallets  # noqa: E402
from paymesh_client import Client  # noqa: E402
from test_e2e import free_port  # noqa: E402

SHOP = "5a" * 32  # a resource server's address: it runs no paymesh code at all


class Facilitator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls._wd = wallets.WALLET_DIR
        wallets.WALLET_DIR = os.path.join(cls.tmp.name, "wallets")
        cls.chain = ch.Chain.open_or_archive(os.path.join(cls.tmp.name, "chain.json"))
        cls.port = free_port()
        cls.api = server.start(cls.chain, port=cls.port)
        cls.url = f"http://127.0.0.1:{cls.port}"
        cls.net = server.NETWORK

    @classmethod
    def tearDownClass(cls):
        cls.api.settler_stop.set()
        cls.api.shutdown()
        cls.api.server_close()
        wallets.WALLET_DIR = cls._wd
        cls.tmp.cleanup()

    def call(self, method, path, body=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
            c.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
            r = c.getresponse()
            return r.status, json.loads(r.read().decode() or "null")
        finally:
            c.close()

    def req(self, amount=7, method="tx", **extra):
        return x402.requirement(self.net, SHOP, amount, method, 60, **extra)

    def body(self, req, payload, accepted=None):
        return {"x402Version": 2, "paymentRequirements": req,
                "paymentPayload": {"x402Version": 2, "accepted": accepted or req, "payload": payload}}

    def signed(self, c, amount=7, memo="for the shop", to=SHOP):
        led = c._ledger()
        tx = led.new_tx(c.wallet.address, to, amount, memo)
        wallets.sign_tx(tx, c.wallet, led)
        return tx

    def test_supported(self):
        code, s = self.call("GET", "/supported")
        self.assertEqual(code, 200)
        self.assertEqual(s["kinds"][0], {**s["kinds"][0], "x402Version": 2, "scheme": "paymesh", "network": self.net})
        self.assertEqual(s["extensions"], [])
        self.assertEqual(s["signers"]["paymesh:*"], [server.HUB_WALLET.address])

    def test_verify_is_read_only_then_settle_once(self):
        c = Client(self.url, "fac-buyer")
        c.faucet()
        tx = self.signed(c)
        b = self.body(self.req(), {"transaction": tx})
        shop0 = self.chain.balance(SHOP)
        for _ in range(2):  # verify changes nothing: no tx pending, nothing spent
            code, v = self.call("POST", "/verify", b)
            self.assertEqual((code, v), (200, {"isValid": True, "payer": c.wallet.address}))
        self.assertEqual((c.balance(), self.chain.find_tx_by_id(tx["txid"])[0]), (100, None))
        code, s = self.call("POST", "/settle", b)
        self.assertEqual((code, s["success"], s["transaction"], s["payer"], s["amount"]),
                         (200, True, tx["txid"], c.wallet.address, "7"))
        self.assertEqual((self.chain.balance(SHOP) - shop0, c.balance()), (7, 92))
        code, again = self.call("POST", "/settle", b)  # the same payment, settled twice: refused
        self.assertEqual((code, again["success"], again["errorReason"]), (200, False, "invalid_transaction_state"))
        self.assertFalse(self.call("POST", "/verify", b)[1]["isValid"])
        self.assertEqual(self.chain.balance(SHOP) - shop0, 7)

    def test_a_txid_payment(self):
        c = Client(self.url, "fac-txid")
        c.faucet()
        tx = self.signed(c, memo="paid ahead")
        c._submit(tx)
        b = self.body(self.req(), {"txid": tx["txid"]})
        self.assertTrue(self.call("POST", "/verify", b)[1]["isValid"])
        self.assertTrue(self.call("POST", "/settle", b)[1]["success"])
        self.assertFalse(self.call("POST", "/settle", b)[1]["success"])

    def test_payments_off_the_requirements_are_invalid(self):
        c = Client(self.url, "fac-off")
        c.faucet()
        req = self.req()
        cases = [
            (self.body(req, {"transaction": self.signed(c, amount=6)}), "invalid_paymesh_payload_value_mismatch"),
            (self.body(req, {"transaction": self.signed(c, to="6b" * 32)}), "invalid_paymesh_payload_recipient_mismatch"),
            (self.body(req, {"transaction": self.signed(c)}, accepted=dict(req, amount="1")), "invalid_payment_requirements"),
            (self.body(dict(req, network="paymesh:" + "0" * 32), {"transaction": self.signed(c)}), "invalid_network"),
            (self.body(dict(req, scheme="exact"), {"transaction": self.signed(c)}), "unsupported_scheme"),
            (self.body(self.req(amount=500), {"transaction": self.signed(c, amount=500)}), "invalid_transaction_state"),
            (self.body(self.req(memo="shop:nonce1:7"), {"transaction": self.signed(c, memo="other")}), "invalid_paymesh_challenge"),
            (self.body(req, {"txid": "ab" * 32}), "invalid_transaction_state"),
            (dict(self.body(req, {"transaction": self.signed(c)}), x402Version=1), "invalid_x402_version"),
        ]
        for b, reason in cases:
            code, v = self.call("POST", "/verify", b)
            self.assertEqual((code, v["isValid"], v["invalidReason"]), (200, False, reason), reason)
            code, s = self.call("POST", "/settle", b)
            self.assertEqual((code, s["success"]), (200, False), reason)
        self.assertEqual(c.balance(), 100)  # nothing moved
        for raw in (b"{", b"[]", b"null", b"\xff"):
            code, _ = self.call("POST", "/settle", raw=raw)
            self.assertIn(code, (200, 400), raw)
        self.assertEqual(self.call("GET", "/health")[0], 200)

    def test_hub_channel_words(self):
        c = Client(self.url, "fac-hub")
        c.faucet()
        c.open_hub(ticks=30)
        cid, i, w = c.channel.pay(7).split()
        b = self.body(self.req(method="channel"), {"channel": cid, "tick": int(i), "word": w})
        self.assertTrue(self.call("POST", "/verify", b)[1]["isValid"])
        self.assertTrue(self.call("POST", "/verify", b)[1]["isValid"])  # verify recorded nothing
        s = self.call("POST", "/settle", b)[1]
        self.assertEqual((s["success"], s["transaction"], s["payer"]), (True, "", c.wallet.address))
        self.assertFalse(self.call("POST", "/settle", b)[1]["success"])  # the word is spent
        before = self.chain.balance(SHOP)
        c.close_channel()
        self.assertEqual(self.chain.balance(SHOP) - before, 7 - 1)  # its share, less the close's fee
        d = Client(self.url, "fac-direct")
        d.faucet()
        d.open_channel(ticks=20)  # to the node's own seller: not the facilitator's to settle
        cid, i, w = d.channel.pay(7).split()
        v = self.call("POST", "/verify", self.body(self.req(method="channel"), {"channel": cid, "tick": int(i), "word": w}))[1]
        self.assertEqual((v["isValid"], v["invalidReason"]), (False, "invalid_paymesh_channel_payment"))

    def test_a_timeout_is_pending_then_settles_once(self):
        c = Client(self.url, "fac-slow")
        c.faucet()
        tx = self.signed(c)
        b = self.body(self.req(), {"transaction": tx})
        real = server.submit_and_wait

        def timed_out(t):
            with server.chain_lock:
                server.chain_inst.add_tx(t)
            with server.mined:
                server.mined.notify_all()
            raise x402.Pending(x402.PENDING_MSG)
        server.submit_and_wait = timed_out
        try:
            s = self.call("POST", "/settle", b)[1]
        finally:
            server.submit_and_wait = real
        self.assertEqual((s["success"], s["errorReason"], s["transaction"]), (False, "settlement_pending", tx["txid"]))
        for _ in range(50):
            if self.chain.find_tx_by_id(tx["txid"])[0]:
                break
            time.sleep(0.05)
        self.assertTrue(self.call("POST", "/settle", b)[1]["success"])  # the retry, once mined
        self.assertFalse(self.call("POST", "/settle", b)[1]["success"])
        self.assertEqual(c.balance(), 100 - 7 - 1)

    def test_settled_marks_survive_a_restart(self):
        c = Client(self.url, "fac-restart")
        c.faucet()
        tx = self.signed(c)
        self.assertTrue(self.call("POST", "/settle", self.body(self.req(), {"transaction": tx}))[1]["success"])
        again = facilitator.Facilitator(self.chain, server.chain_lock, server.submit_and_wait, server.HUB,
                                        server.HUB_WALLET.address, self.net, server.FAC.log.path)
        self.assertIn(tx["txid"], again.settled)
        s = again.settle(self.body(self.req(), {"txid": tx["txid"]}))
        self.assertEqual((s["success"], s["errorReason"]), (False, "invalid_transaction_state"))


if __name__ == "__main__":
    unittest.main()
