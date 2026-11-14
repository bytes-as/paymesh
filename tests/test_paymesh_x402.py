"""x402 v2 wire format (PAYMENT-REQUIRED / PAYMENT-SIGNATURE / PAYMENT-RESPONSE) for the
"paymesh" scheme: header shapes per the spec, and the node refusing bad x402 payments."""
import base64
import http.client
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chain as ch  # noqa: E402
import server  # noqa: E402
import wallets  # noqa: E402
import paymesh_x402 as x402  # noqa: E402
from paymesh_client import Client  # noqa: E402
from test_e2e import free_port  # noqa: E402

SELLER = "ab" * 32
NET = "paymesh:" + "cd" * 16


def sig(accepted, payload, version=2):
    return x402.encode({"x402Version": version, "accepted": accepted, "payload": payload})


class Wire(unittest.TestCase):
    """Pure functions: no server."""

    def offers(self, price=5):
        return x402.paymesh_accepts(NET, SELLER, price, {"memo": f"{SELLER}:{'0' * 32}:{price}", "nonce": "0" * 32,
                                                         "expires_in": 300}, 1)

    def test_payment_required_has_every_field_the_spec_requires(self):
        req = x402.payment_required("http://h/premium/price", self.offers())
        self.assertEqual(req["x402Version"], 2)
        self.assertEqual(req["resource"]["url"], "http://h/premium/price")
        for a in req["accepts"]:
            for k, t in (("scheme", str), ("network", str), ("amount", str), ("asset", str), ("payTo", str),
                         ("maxTimeoutSeconds", int), ("extra", dict)):
                self.assertIsInstance(a[k], t, k)
            self.assertRegex(a["network"], r"^[-a-z0-9]{3,8}:[-_a-zA-Z0-9]{1,32}$")  # CAIP-2
            self.assertEqual(a["extra"]["paymentFlow"], "upfront")  # not "authorization": must be present (spec 6.1)
        self.assertEqual({a["extra"]["assetTransferMethod"] for a in req["accepts"]}, {"tx", "channel"})
        self.assertEqual(x402.decode(x402.encode(req)), req)

    def test_settlement_response_shape(self):
        ok = x402.settlement(NET, True, "ef" * 32, SELLER, 5)
        self.assertEqual(set(ok), {"success", "transaction", "network", "payer", "amount"})
        bad = x402.settlement(NET, False, reason="insufficient_funds", message="m")
        self.assertEqual(bad["transaction"], "")  # required, empty when nothing was broadcast
        self.assertEqual(bad["errorReason"], "insufficient_funds")

    def test_decode_refuses_garbage(self):
        for v in ["", "   ", "!!!", base64.b64encode(b"[1]").decode(), base64.b64encode(b"\xff\xfe").decode(),
                  "A" * (x402.MAX_HEADER + 4)]:
            with self.assertRaises(x402.Invalid):
                x402.decode(v)

    def test_parse_refuses_anything_off_the_quote(self):
        tx_offer, ch_offer = self.offers()
        good_tx = {"transaction": {"to_wallet": SELLER, "amount": 5, "memo": "m", "txid": "t"}}
        self.assertEqual(x402.parse(sig(tx_offer, good_tx), NET, SELLER, 5)[0], "tx")
        cases = [
            (sig(tx_offer, good_tx, version=1), "invalid_x402_version"),
            (sig(dict(tx_offer, scheme="exact"), good_tx), "unsupported_scheme"),
            (sig(dict(tx_offer, network="paymesh:other"), good_tx), "invalid_network"),
            (sig(dict(tx_offer, amount="1"), good_tx), "invalid_payment_requirements"),
            (sig(dict(tx_offer, payTo="ee" * 32), good_tx), "invalid_payment_requirements"),
            (sig(dict(tx_offer, extra={"assetTransferMethod": "magic"}), good_tx), "invalid_payment_requirements"),
            (sig(dict(tx_offer, extra={"paymentFlow": "authorization"}), good_tx), "invalid_payment_requirements"),
            (sig(tx_offer, {"transaction": dict(good_tx["transaction"], to_wallet="ee" * 32)}),
             "invalid_paymesh_payload_recipient_mismatch"),
            (sig(tx_offer, {"transaction": dict(good_tx["transaction"], amount=1)}), "invalid_paymesh_payload_value_mismatch"),
            (sig(tx_offer, {"transaction": dict(good_tx["transaction"], amount=5.0)}), "invalid_paymesh_payload_value_mismatch"),
            (sig(tx_offer, {"transaction": dict(good_tx["transaction"], kind="channel_open")}), "invalid_payload"),
            (sig(tx_offer, {"txid": "XY"}), "invalid_payload"),
            (sig(tx_offer, {"txid": "ab" * 32, "more": 1}), "invalid_payload"),
            (sig(ch_offer, {"channel": "ab" * 32, "tick": True, "word": "cd" * 32}), "invalid_payload"),
            (sig(ch_offer, {"channel": "ab" * 32, "tick": 0, "word": "cd" * 32}), "invalid_payload"),
            (sig(ch_offer, {"channel": "ab" * 32, "tick": 3, "word": "cd" * 32, "x": 1}), "invalid_payload"),
            (sig(ch_offer, {"channel": "ab cd", "tick": 3, "word": "cd" * 32}), "invalid_payload"),
        ]
        for value, reason in cases:
            with self.assertRaises(x402.Invalid, msg=reason) as cm:
                x402.parse(value, NET, SELLER, 5)
            self.assertEqual(cm.exception.reason, reason)


class Node(unittest.TestCase):
    """A real node on scratch ports, spoken to in x402 headers."""

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

    def raw(self, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            c.request("GET", path, headers=headers or {})
            r = c.getresponse()
            return r.status, dict(r.getheaders()), json.loads(r.read().decode() or "null")
        finally:
            c.close()

    def challenge(self, path="/premium/price"):
        code, h, _ = self.raw(path)
        self.assertEqual(code, 402)
        req = x402.decode(h[x402.REQUIRED])
        return req, {a["extra"]["assetTransferMethod"]: a for a in req["accepts"]}

    def test_402_carries_payment_required(self):
        req, offers = self.challenge()
        self.assertEqual(req["resource"]["url"], f"http://127.0.0.1:{self.port}/premium/price")
        self.assertEqual(offers["tx"]["amount"], "5")
        self.assertEqual(offers["tx"]["network"], x402.network_id(self.chain.blocks[0].hash()))
        self.assertEqual(self.raw("/health")[2]["network"], offers["tx"]["network"])

    def test_client_pays_in_one_request_and_gets_a_receipt(self):
        c = Client(self.url, "x402-buyer")
        c.faucet()
        code, body = c.get("/premium/price")
        self.assertEqual(code, 200, body)
        r = c.last_receipt()
        self.assertTrue(r["success"])
        self.assertEqual(r["payer"], c.wallet.address)
        self.assertEqual(r["amount"], "5")
        tx, _ = self.chain.find_tx_by_id(r["transaction"])  # the seller put the buyer's tx on chain
        self.assertEqual((tx["from_wallet"], tx["amount"]), (c.wallet.address, 5))
        c.open_channel(ticks=10)
        self.assertEqual(c.get("/premium/price")[0], 200)
        r = c.last_receipt()
        self.assertEqual((r["success"], r["transaction"], r["payer"]), (True, "", c.wallet.address))
        self.assertEqual(c.close_channel()["paid_to_seller"], 5)

    def test_replayed_payment_signature_is_refused_and_moves_no_money(self):
        c = Client(self.url, "x402-replay")
        c.faucet()
        _, offers = self.challenge()
        led = c._ledger()
        tx = led.new_tx(c.wallet.address, offers["tx"]["payTo"], 5, offers["tx"]["extra"]["memo"])
        wallets.sign_tx(tx, c.wallet, led)
        h = {x402.SIGNATURE: sig(offers["tx"], {"transaction": tx})}
        self.assertEqual(self.raw("/premium/price", h)[0], 200)
        code, hdrs, _ = self.raw("/premium/price", h)
        self.assertEqual(code, 402)
        self.assertFalse(x402.decode(hdrs[x402.RESPONSE])["success"])
        self.assertIn(x402.REQUIRED, hdrs)  # a fresh challenge to pay again
        self.assertEqual(c.balance(), 95)

    def test_tx_for_a_spent_or_foreign_challenge_is_not_submitted(self):
        c = Client(self.url, "x402-late")
        c.faucet()
        _, offers = self.challenge("/premium/history")  # a 20 PMX challenge...
        led = c._ledger()
        memo = offers["tx"]["extra"]["memo"].rsplit(":", 1)[0] + ":5"  # ...turned into 5 PMX for /premium/price
        tx = led.new_tx(c.wallet.address, offers["tx"]["payTo"], 5, memo)
        wallets.sign_tx(tx, c.wallet, led)
        _, price_offers = self.challenge()
        code, hdrs, _ = self.raw("/premium/price", {x402.SIGNATURE: sig(price_offers["tx"], {"transaction": tx})})
        self.assertEqual(code, 402)
        self.assertEqual(x402.decode(hdrs[x402.RESPONSE])["errorReason"], "invalid_paymesh_challenge")
        self.assertEqual(c.balance(), 100)  # refused before it was put on chain
        self.assertIsNone(self.chain.find_tx_by_id(tx["txid"])[0])

    def test_malformed_payment_signature_is_400(self):
        code, hdrs, body = self.raw("/premium/price", {x402.SIGNATURE: "not base64!"})
        self.assertEqual(code, 400)
        self.assertEqual(x402.decode(hdrs[x402.RESPONSE])["errorReason"], "invalid_payload")
        self.assertEqual(self.raw("/health")[0], 200)

    def test_legacy_headers_still_pay(self):
        c = Client(self.url, "x402-legacy")
        c.faucet()
        _, offers = self.challenge()
        led = c._ledger()
        tx = led.new_tx(c.wallet.address, offers["tx"]["payTo"], 5, offers["tx"]["extra"]["memo"])
        wallets.sign_tx(tx, c.wallet, led)
        c._submit(tx)
        self.assertEqual(self.raw("/premium/price", {"X-Payment": tx["txid"]})[0], 200)


if __name__ == "__main__":
    unittest.main()
