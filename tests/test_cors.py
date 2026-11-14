"""Browsers: the paid x402 routes are open to any origin (preflight answered, PAYMENT-REQUIRED and
PAYMENT-RESPONSE exposed); every other route refuses a request a browser marks as cross-site, so a
foreign page can't mint, open or close channels, settle, or read accounts through a visitor's
browser. Clients that are not browsers (no Origin, no Sec-Fetch-Site) are unaffected."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from wsgiref.simple_server import make_server

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "examples")]

import chain as ch  # noqa: E402
import paymesh_x402 as x402  # noqa: E402
import server  # noqa: E402
import wallets  # noqa: E402
import weather_api  # noqa: E402
from paymesh_client import Client  # noqa: E402
from test_e2e import free_port  # noqa: E402
from test_seller import Quiet  # noqa: E402

EVIL = "https://evil.example"
CROSS = {"Origin": EVIL, "Sec-Fetch-Site": "cross-site"}


def raw(port, method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data
    finally:
        c.close()


class CorsNode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls._wd = wallets.WALLET_DIR
        wallets.WALLET_DIR = os.path.join(cls.tmp.name, "wallets")
        cls.chain = ch.Chain.open_or_archive(os.path.join(cls.tmp.name, "chain.json"))
        cls.port = free_port()
        cls.api = server.start(cls.chain, port=cls.port)
        cls.url = f"http://127.0.0.1:{cls.port}"
        cls.sport = free_port()
        cls.paywall, app = weather_api.build(cls.url, wallet="cors-shop")
        cls.httpd = make_server("127.0.0.1", cls.sport, app, server_class=weather_api.ThreadingWSGIServer, handler_class=Quiet)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.paywall.stop.set()
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.api.settler_stop.set()
        cls.api.shutdown()
        cls.api.server_close()
        wallets.WALLET_DIR = cls._wd
        cls.tmp.cleanup()

    def assert_open(self, h):
        self.assertEqual(h.get("access-control-allow-origin"), "*")
        exposed = {s.strip().upper() for s in h.get("access-control-expose-headers", "").split(",")}
        self.assertLessEqual({"PAYMENT-REQUIRED", "PAYMENT-RESPONSE"}, exposed)

    def test_preflight_on_paid_routes(self):
        for port, path in ((self.port, "/premium/price"), (self.port, "/premium/history"), (self.sport, "/forecast")):
            code, h, body = raw(port, "OPTIONS", path, dict(CROSS, **{"Access-Control-Request-Method": "GET",
                                                                     "Access-Control-Request-Headers": "payment-signature"}))
            self.assertEqual((code, body), (204, b""), path)
            self.assert_open(h)
            self.assertIn("GET", h["access-control-allow-methods"])
            self.assertIn("PAYMENT-SIGNATURE", h["access-control-allow-headers"].upper())

    def test_a_foreign_page_pays_and_reads_the_receipt(self):
        c = Client(self.url, "cors-buyer")
        c.faucet()
        for port, path, price in ((self.port, "/premium/price", 5), (self.sport, "/forecast", 3)):
            code, h, _ = raw(port, "GET", path, CROSS)
            self.assertEqual(code, 402, path)
            self.assert_open(h)
            accepted = next(a for a in x402.decode(h["payment-required"])["accepts"]
                            if a["extra"]["assetTransferMethod"] == "tx")
            led = c._ledger()
            tx = led.new_tx(c.wallet.address, accepted["payTo"], price, accepted["extra"]["memo"])
            wallets.sign_tx(tx, c.wallet, led)
            sig = x402.encode({"x402Version": 2, "accepted": accepted, "payload": {"transaction": tx}})
            code, h, _ = raw(port, "GET", path, dict(CROSS, **{"PAYMENT-SIGNATURE": sig}))
            self.assertEqual(code, 200, path)
            self.assert_open(h)
            self.assertTrue(x402.decode(h["payment-response"])["success"])

    def test_other_routes_are_closed_to_other_sites(self):
        victim = Client(self.url, "cors-victim")
        victim.faucet()
        before = self.chain.height
        addr = victim.wallet.address
        led = victim._ledger()
        tx = led.new_tx(addr, "5a" * 32, 1, "x")
        wallets.sign_tx(tx, victim.wallet, led)
        txt = {"Content-Type": "text/plain"}  # a "simple" request: no preflight, the browser just sends it
        cases = [(self.port, "GET", f"/faucet?address={addr}", None), (self.port, "GET", f"/account?address={addr}", None),
                 (self.port, "GET", "/stats", None), (self.port, "POST", "/tx", json.dumps(tx)),
                 (self.port, "POST", "/channel/close", '{"channel": "x"}'),
                 (self.port, "POST", "/settle", "{}"), (self.port, "POST", "/hub/claim", "{}"),
                 (self.sport, "POST", "/channel/close", '{"channel": "x"}'), (self.sport, "GET", "/channel/terms", None)]
        for hdrs in (CROSS, {"Origin": EVIL}, {"Sec-Fetch-Site": "same-site"}, {"Origin": "null"}):
            for port, method, path, body in cases:
                code, h, _ = raw(port, method, path, dict(hdrs, **txt), body)
                self.assertEqual(code, 403, (hdrs, path))
                self.assertNotIn("access-control-allow-origin", h, path)
            for port, method, path, _ in cases:  # and no preflight passes for them
                code, h, _ = raw(port, "OPTIONS", path, dict(hdrs, **{"Access-Control-Request-Method": method}))
                self.assertEqual(code, 403, path)
                self.assertNotIn("access-control-allow-origin", h, path)
        self.assertEqual((self.chain.height, victim.balance()), (before, 100))  # nothing minted, nothing mined
        self.assertIsNone(self.chain.find_tx_by_id(tx["txid"])[0])

    def test_same_origin_and_non_browser_clients_still_work(self):
        addr = "7c" * 32
        code, h, _ = raw(self.port, "GET", f"/faucet?address={addr}")  # a script: no browser headers
        self.assertEqual(code, 200)
        self.assertNotIn("access-control-allow-origin", h)
        same = {"Origin": f"http://127.0.0.1:{self.port}", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(raw(self.port, "GET", f"/account?address={addr}", same)[0], 200)
        self.assertEqual(raw(self.port, "GET", f"/faucet?address={addr}", {"Sec-Fetch-Site": "none"})[0], 200)  # typed URL
        self.assertEqual(self.chain.balance(addr), 200)

    def test_cors_does_not_leak_onto_the_next_keep_alive_response(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            for path, cors in (("/open/info", True), ("/health", False), ("/premium/price", True), ("/channel/terms", False)):
                c.request("GET", path)
                r = c.getresponse()
                r.read()
                self.assertEqual(r.getheader("Access-Control-Allow-Origin") == "*", cors, path)
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
