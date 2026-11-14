"""x402 v2 wire format (PAYMENT-REQUIRED / PAYMENT-SIGNATURE / PAYMENT-RESPONSE) for the
"paymesh" scheme: header shapes per the spec, and the node refusing bad x402 payments."""
import base64
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
        self.assertEqual(c.balance(), 94)  # once: 5 + a 1 PMX fee

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

    def test_refused_tx_retires_the_key_that_signed_it(self):
        """A seller that refuses a signed tx (and might keep it) never sees a second signature
        from that one-time key: the client rotates it with a self-payment first."""
        from paymesh_client import PaymentError
        c = Client(self.url, "x402-retire")
        c.faucet()
        _, offers = self.challenge()
        bogus = dict(offers["tx"], extra=dict(offers["tx"]["extra"], memo=f"{offers['tx']['payTo']}:{'0' * 32}:5"))
        i = c.wallet.index_of(self.chain.current_key(c.wallet.address))
        with self.assertRaises(PaymentError):
            c._on_chain("/premium/price", bogus)  # unknown nonce: refused before it is put on chain
        # the self-pay (key i, sent only to the node) moved the ledger to key i+1: the seller's
        # copy, signed by key i, can never be mined or extended
        self.assertEqual(self.chain.current_key(c.wallet.address), c.wallet.key_fp(i + 1))
        self.assertEqual(c.balance(), 99)  # the self-payment costs only its 1 PMX fee
        self.assertEqual(c.get("/premium/price")[0], 200)  # and paying works on the next key

    def test_two_txs_for_one_challenge_at_once_charge_once(self):
        """Two PAYMENT-SIGNATUREs carrying different txs for the same challenge, sent at once
        (QA-PM-4): the challenge is reserved by the first, the second never goes on chain."""
        import threading
        c = Client(self.url, "x402-twice")
        c.faucet()
        _, offers = self.challenge()
        led = c._ledger()
        heads, delay = [], {}
        for n in range(2):
            tx = led.new_tx(c.wallet.address, offers["tx"]["payTo"], 5, offers["tx"]["extra"]["memo"])
            delay[tx["txid"]] = 0.3 * (n + 1)  # submitted in key order, so without the reservation both would be valid
            wallets.sign_tx(tx, c.wallet, led)
            led._key = tx["next_key"]  # the second is signed by the next one-time key
            heads.append({x402.SIGNATURE: sig(offers["tx"], {"transaction": tx})})
        gate, codes = threading.Barrier(2), []

        def pay(h):
            gate.wait()
            codes.append(self.raw("/premium/price", h)[0])
        real = server.submit_and_wait

        def slow_submit(tx):  # widen the race: both requests check the challenge before either submits
            time.sleep(delay.get(tx.get("txid"), 0))
            return real(tx)
        server.submit_and_wait = slow_submit
        try:
            ts = [threading.Thread(target=pay, args=(h,)) for h in heads]
            [t.start() for t in ts]
            [t.join() for t in ts]
        finally:
            server.submit_and_wait = real
        mined = [t for t in self.chain.all_txs() if t["from_wallet"] == c.wallet.address]
        # usually [200, 402]; if the second (next-key) tx reserves first, the ledger refuses it and
        # both get 402 with nothing charged. Never: two mined, or a charge without a served call
        self.assertIn(sorted(codes), ([200, 402], [402, 402]))
        self.assertEqual(len(mined), codes.count(200))
        self.assertEqual(c.balance(), 100 - (5 + 1) * len(mined))

    def test_a_mining_timeout_keeps_the_challenge_reserved(self):
        """QA-PM-4 follow-up: the node answers "not mined in time" but the tx is queued and is
        mined later. Another tx for the same challenge must stay refused; the same tx, retried,
        is served once. The buyer pays once."""
        c = Client(self.url, "x402-timeout")
        c.faucet()
        _, offers = self.challenge()
        led = c._ledger()
        txs = []
        for _ in range(2):
            t = led.new_tx(c.wallet.address, offers["tx"]["payTo"], 5, offers["tx"]["extra"]["memo"])
            wallets.sign_tx(t, c.wallet, led)
            led._key = t["next_key"]
            txs.append(t)
        real = server.submit_and_wait

        def timed_out(tx):  # accepted and queued, then the wait runs out
            with server.chain_lock:
                server.chain_inst.add_tx(tx)
            with server.mined:
                server.mined.notify_all()
            raise x402.Pending(x402.PENDING_MSG)
        server.submit_and_wait = timed_out
        try:
            code, hdrs, _ = self.raw("/premium/price", {x402.SIGNATURE: sig(offers["tx"], {"transaction": txs[0]})})
        finally:
            server.submit_and_wait = real
        receipt = x402.decode(hdrs[x402.RESPONSE])
        self.assertEqual((code, receipt["errorReason"], receipt["transaction"]), (402, "settlement_pending", txs[0]["txid"]))
        code, hdrs, _ = self.raw("/premium/price", {x402.SIGNATURE: sig(offers["tx"], {"transaction": txs[1]})})
        self.assertEqual(code, 402)
        self.assertIn("another tx is already paying", x402.decode(hdrs[x402.RESPONSE])["errorMessage"])
        for _ in range(50):  # the queued tx gets mined
            if self.chain.find_tx_by_id(txs[0]["txid"])[0]:
                break
            time.sleep(0.05)
        code, _, _ = self.raw("/premium/price", {x402.SIGNATURE: sig(offers["tx"], {"transaction": txs[0]})})
        self.assertEqual(code, 200)  # the retry is served
        self.assertIsNone(self.chain.find_tx_by_id(txs[1]["txid"])[0])
        self.assertEqual(c.balance(), 100 - 5 - 1)

    def test_open_a_channel_inside_the_payment(self):
        """A client that can't post /tx (a browser page) opens its channel in PAYMENT-SIGNATURE."""
        import channels
        c = Client(self.url, "x402-opener")
        c.faucet()
        _, offers = self.challenge()
        chan = offers["channel"]
        self.assertIsInstance(chan["extra"]["height"], int)

        def opening(to=None, kind="channel_open", extra=None):
            bc = channels.BuyerChannel(20)
            led = c._ledger()
            tx = led.new_tx(c.wallet.address, to or chan["payTo"], 20, "channel:open")
            tx.update(kind=kind, anchor=bc.anchor, ticks=20, unit=1, expires=chan["extra"]["height"] + 100)
            wallets.sign_tx(tx, c.wallet, led)
            return bc, dict({"open": tx, "tick": 5, "word": bc.words[5]}, **(extra or {}))
        bc, payload = opening(to="5a" * 32)  # to someone else: refused before anything is mined
        code, hdrs, _ = self.raw("/premium/price", {x402.SIGNATURE: sig(chan, payload)})
        self.assertEqual(code, 402)
        self.assertEqual(x402.decode(hdrs[x402.RESPONSE])["errorReason"], "invalid_paymesh_payload_recipient_mismatch")
        self.assertIsNone(self.chain.find_tx_by_id(payload["open"]["txid"])[0])
        _, payload = opening(extra={"channel": "ab" * 32})
        self.assertEqual(self.raw("/premium/price", {x402.SIGNATURE: sig(chan, payload)})[0], 400)
        c._retire_key(payload["open"])  # that key was seen: move past it before signing again
        bc, payload = opening()
        code, hdrs, body = self.raw("/premium/price", {x402.SIGNATURE: sig(chan, payload)})
        self.assertEqual(code, 200, body)
        cid = payload["open"]["txid"]
        self.assertEqual(self.chain.ledger.channels[cid]["buyer"], c.wallet.address)
        code, _, _ = self.raw("/premium/price", {x402.SIGNATURE: sig(chan, payload)})  # the same again: word spent
        self.assertEqual(code, 402)
        nxt = {"channel": cid, "tick": 10, "word": bc.words[10]}
        self.assertEqual(self.raw("/premium/price", {x402.SIGNATURE: sig(chan, nxt)})[0], 200)

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


# verbatim from specs/transports-v2/http.md at x402-foundation/x402@31ce6bb
SPEC_EXAMPLES = [
    ("PAYMENT-REQUIRED", "eyJ4NDAyVmVyc2lvbiI6MiwiZXJyb3IiOiJQQVlNRU5ULVNJR05BVFVSRSBoZWFkZXIgaXMgcmVxdWlyZWQiLCJyZXNvdXJjZSI6eyJ1cmwiOiJodHRwczovL2FwaS5leGFtcGxlLmNvbS9wcmVtaXVtLWRhdGEiLCJkZXNjcmlwdGlvbiI6IkFjY2VzcyB0byBwcmVtaXVtIG1hcmtldCBkYXRhIiwibWltZVR5cGUiOiJhcHBsaWNhdGlvbi9qc29uIn0sImFjY2VwdHMiOlt7InNjaGVtZSI6ImV4YWN0IiwibmV0d29yayI6ImVpcDE1NTo4NDUzMiIsImFtb3VudCI6IjEwMDAwIiwiYXNzZXQiOiIweDAzNkNiRDUzODQyYzU0MjY2MzRlNzkyOTU0MWVDMjMxOGYzZENGN2UiLCJwYXlUbyI6IjB4MjA5NjkzQmM2YWZjMEM1MzI4YkEzNkZhRjAzQzUxNEVGMzEyMjg3QyIsIm1heFRpbWVvdXRTZWNvbmRzIjo2MCwiZXh0cmEiOnsibmFtZSI6IlVTREMiLCJ2ZXJzaW9uIjoiMiJ9fV19"),
    ("PAYMENT-SIGNATURE", "eyJ4NDAyVmVyc2lvbiI6MiwicmVzb3VyY2UiOnsidXJsIjoiaHR0cHM6Ly9hcGkuZXhhbXBsZS5jb20vcHJlbWl1bS1kYXRhIiwiZGVzY3JpcHRpb24iOiJBY2Nlc3MgdG8gcHJlbWl1bSBtYXJrZXQgZGF0YSIsIm1pbWVUeXBlIjoiYXBwbGljYXRpb24vanNvbiJ9LCJhY2NlcHRlZCI6eyJzY2hlbWUiOiJleGFjdCIsIm5ldHdvcmsiOiJlaXAxNTU6ODQ1MzIiLCJhbW91bnQiOiIxMDAwMCIsImFzc2V0IjoiMHgwMzZDYkQ1Mzg0MmM1NDI2NjM0ZTc5Mjk1NDFlQzIzMThmM2RDRjdlIiwicGF5VG8iOiIweDIwOTY5M0JjNmFmYzBDNTMyOGJBMzZGYUYwM0M1MTRFRjMxMjI4N0MiLCJtYXhUaW1lb3V0U2Vjb25kcyI6NjAsImV4dHJhIjp7Im5hbWUiOiJVU0RDIiwidmVyc2lvbiI6IjIifX0sInBheWxvYWQiOnsic2lnbmF0dXJlIjoiMHgyZDZhNzU4OGQ2YWNjYTUwNWNiZjBkOWE0YTIyN2UwYzUyYzZjMzQwMDhjOGU4OTg2YTEyODMyNTk3NjQxNzM2MDhhMmNlNjQ5NjY0MmUzNzdkNmRhOGRiYmY1ODM2ZTliZDE1MDkyZjllY2FiMDVkZWQzZDYyOTNhZjE0OGI1NzFjIiwiYXV0aG9yaXphdGlvbiI6eyJmcm9tIjoiMHg4NTdiMDY1MTlFOTFlM0E1NDUzODc5MWJEYmIwRTIyMzczZTM2YjY2IiwidG8iOiIweDIwOTY5M0JjNmFmYzBDNTMyOGJBMzZGYUYwM0M1MTRFRjMxMjI4N0MiLCJ2YWx1ZSI6IjEwMDAwIiwidmFsaWRBZnRlciI6IjE3NDA2NzIwODkiLCJ2YWxpZEJlZm9yZSI6IjE3NDA2NzIxNTQiLCJub25jZSI6IjB4ZjM3NDY2MTNjMmQ5MjBiNWZkYWJjMDg1NmYyYWViMmQ0Zjg4ZWU2MDM3YjhjYzVkMDRhNzFhNDQ2MmYxMzQ4MCJ9fX0="),
    ("PAYMENT-RESPONSE", "eyJzdWNjZXNzIjp0cnVlLCJ0cmFuc2FjdGlvbiI6IjB4MTIzNDU2Nzg5MGFiY2RlZjEyMzQ1Njc4OTBhYmNkZWYxMjM0NTY3ODkwYWJjZGVmMTIzNDU2Nzg5MGFiY2RlZiIsIm5ldHdvcmsiOiJlaXAxNTU6ODQ1MzIiLCJwYXllciI6IjB4ODU3YjA2NTE5RTkxZTNBNTQ1Mzg3OTFiRGJiMEUyMjM3M2UzNmI2NiJ9"),
    ("PAYMENT-RESPONSE", "eyJzdWNjZXNzIjpmYWxzZSwiZXJyb3JSZWFzb24iOiJpbnN1ZmZpY2llbnRfZnVuZHMiLCJ0cmFuc2FjdGlvbiI6IiIsIm5ldHdvcmsiOiJlaXAxNTU6ODQ1MzIiLCJwYXllciI6IjB4ODU3YjA2NTE5RTkxZTNBNTQ1Mzg3OTFiRGJiMEUyMjM3M2UzNmI2NiJ9"),
]


class SpecExamples(unittest.TestCase):
    """The spec's own base64 header examples, decoded with our decoder, and our encoder's
    output decoded back to the same objects (key for key)."""

    def test_spec_examples_decode_to_the_documented_shapes(self):
        got = {}
        for name, value in SPEC_EXAMPLES:
            obj = x402.decode(value)
            self.assertEqual(x402.decode(x402.encode(obj)), obj)
            got.setdefault(name, []).append(obj)
        req = got["PAYMENT-REQUIRED"][0]
        self.assertEqual(req["x402Version"], 2)
        self.assertEqual(set(req["accepts"][0]), {"scheme", "network", "amount", "asset", "payTo", "maxTimeoutSeconds", "extra"})
        self.assertEqual(set(x402.requirement(NET, SELLER, 5, "tx", 60)), set(req["accepts"][0]))
        sig = got["PAYMENT-SIGNATURE"][0]
        self.assertEqual(set(sig) - {"resource"}, {"x402Version", "accepted", "payload"})
        ok, bad = got["PAYMENT-RESPONSE"]
        self.assertEqual(set(x402.settlement("n", True, "t", "p")), set(ok))
        self.assertEqual(set(x402.settlement("n", False, payer="p", reason="insufficient_funds")), set(bad))

    def test_spec_exact_payment_is_refused_as_another_scheme(self):
        sig = dict(SPEC_EXAMPLES)["PAYMENT-SIGNATURE"]
        with self.assertRaises(x402.Invalid) as cm:
            x402.parse(sig, NET, SELLER, 5)
        self.assertEqual(cm.exception.reason, "unsupported_scheme")


if __name__ == "__main__":
    unittest.main()
