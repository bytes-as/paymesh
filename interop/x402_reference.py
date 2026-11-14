"""Interop check: the reference x402 Python SDK paying a scratch paymesh node.

paymesh itself stays stdlib-only; this one script needs the reference SDK, so run it
from a separate venv (never installed into paymesh, not run by check.py):

    py -3 -m venv %TEMP%\\x402ref
    %TEMP%\\x402ref\\Scripts\\python.exe -m pip install "x402[requests]==2.27.0"
    %TEMP%\\x402ref\\Scripts\\python.exe interop\\x402_reference.py

Last run: x402 2.27.0, REFERENCE INTEROP OK (2026-11-15: fees, the facilitator client, an opening payload).
The facilitator part needs httpx too: pip install "x402[requests,httpx]==2.27.0".
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

PAYMESH = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PAYMESH)

import requests  # noqa: E402
from x402 import x402ClientSync  # noqa: E402
from x402.http.clients import wrapRequestsWithPayment  # noqa: E402
from x402.http.utils import (decode_payment_required_header, decode_payment_response_header,  # noqa: E402
                             encode_payment_signature_header)
from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync  # noqa: E402
from x402.schemas import PaymentPayload, PaymentRequirements  # noqa: E402

import wallets  # noqa: E402  (paymesh, stdlib only)
from paymesh_client import Client  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PaymeshTx:
    """SchemeNetworkClient for scheme "paymesh", method "tx": sign a pay tx, the server settles it."""
    scheme = "paymesh"

    def __init__(self, pm: Client):
        self.pm = pm

    def create_payment_payload(self, requirements):
        led = self.pm._ledger()
        tx = led.new_tx(self.pm.wallet.address, requirements.pay_to, int(requirements.amount), requirements.extra["memo"])
        wallets.sign_tx(tx, self.pm.wallet, led)
        return {"transaction": tx}


def main():
    home, port = tempfile.mkdtemp(prefix="paymesh-interop-"), free_port()
    env = dict(os.environ, PAYMESH_HOME=home, PAYMESH_API_PORT=str(port), PAYMESH_EXPLORER_PORT=str(free_port()))
    node = subprocess.Popen(["py", "-3", os.path.join(PAYMESH, "paymesh.py"), "serve"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "/health", timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        wallets.WALLET_DIR = os.path.join(home, "client-wallets")
        pm = Client(url, "ref-buyer")
        pm.faucet()
        out = {}

        # 1. the reference SDK parses our PAYMENT-REQUIRED (pydantic models)
        r = requests.get(url + "/premium/price")
        req = decode_payment_required_header(r.headers["PAYMENT-REQUIRED"])
        out["payment_required"] = {"status": r.status_code, "version": req.x402_version,
                                   "accepts": [(a.scheme, a.network, a.amount, a.extra.get("assetTransferMethod"))
                                               for a in req.accepts]}

        # 2. the reference requests wrapper pays end to end through our scheme plugin
        client = x402ClientSync()
        net = req.accepts[0].network  # PMX isn't a default asset: allow it, capped at 10 per payment
        client._spend_controls = {"allowed_assets": [{"network": net, "asset": "PMX", "max_amount_per_payment": "10"}]}
        client.register(req.accepts[0].network, PaymeshTx(pm))
        session = wrapRequestsWithPayment(requests.Session(), client)
        r = session.get(url + "/premium/price")
        receipt = decode_payment_response_header(r.headers["PAYMENT-RESPONSE"])
        out["paid_by_reference_client"] = {"status": r.status_code, "success": receipt.success,
                                           "transaction": receipt.transaction[:16], "payer_is_buyer":
                                           receipt.payer == pm.wallet.address, "body": r.json().get("paid")}
        out["balance_after"] = pm.balance()

        # 3. a channel payment encoded by the reference SDK's own PaymentPayload + header encoder
        pm.open_channel(ticks=10)
        r = requests.get(url + "/premium/price")
        req = decode_payment_required_header(r.headers["PAYMENT-REQUIRED"])
        chan_req = next(a for a in req.accepts if a.extra.get("assetTransferMethod") == "channel")
        cid, i, w = pm.channel.pay(5).split()
        payload = PaymentPayload(x402_version=2, accepted=chan_req, payload={"channel": cid, "tick": int(i), "word": w})
        r = requests.get(url + "/premium/price", headers={"PAYMENT-SIGNATURE": encode_payment_signature_header(payload)})
        receipt = decode_payment_response_header(r.headers["PAYMENT-RESPONSE"])
        out["channel_via_reference_encoder"] = {"status": r.status_code, "success": receipt.success}
        r = requests.get(url + "/premium/price", headers={"PAYMENT-SIGNATURE": encode_payment_signature_header(payload)})
        out["channel_replay"] = {"status": r.status_code,
                                 "errorReason": decode_payment_response_header(r.headers["PAYMENT-RESPONSE"]).error_reason}
        # 4. the node as an x402 facilitator, driven by the reference facilitator client
        fac = HTTPFacilitatorClientSync(FacilitatorConfig(url=url))
        sup = fac.get_supported()
        out["facilitator_supported"] = [(k.scheme, k.network) for k in sup.kinds]
        shop = "5a" * 32  # a resource server running no paymesh code
        req = PaymentRequirements(scheme="paymesh", network=sup.kinds[0].network, amount="7", asset="PMX", pay_to=shop,
                                  max_timeout_seconds=60, extra={"assetTransferMethod": "tx", "paymentFlow": "upfront",
                                                                 "memo": "shop:order-9:7"})
        led = pm._ledger()
        tx = led.new_tx(pm.wallet.address, shop, 7, "shop:order-9:7")
        wallets.sign_tx(tx, pm.wallet, led)
        pay = PaymentPayload(x402_version=2, accepted=req, payload={"transaction": tx})
        v = fac.verify(pay, req)
        s1 = fac.settle(pay, req)
        s2 = fac.settle(pay, req)
        out["facilitator"] = {"verify": [v.is_valid, v.payer == pm.wallet.address],
                              "settle": [s1.success, s1.transaction == tx["txid"]],
                              "settle_again": [s2.success, s2.error_reason]}
        # 5. an opening payload through the reference facilitator client: a hub channel opened
        # inside the payment ({"open": signed channel_open, "tick", "word"}); payload is free JSON
        import channels
        hub = sup.kinds[0].extra["hub"]
        creq = PaymentRequirements(scheme="paymesh", network=sup.kinds[0].network, amount="7", asset="PMX", pay_to=shop,
                                   max_timeout_seconds=60, extra={"assetTransferMethod": "channel", "paymentFlow": "upfront",
                                                                  "unit": 1})
        bc = channels.BuyerChannel(30)
        led = pm._ledger()
        op = led.new_tx(pm.wallet.address, hub, 30, "channel:open")
        op.update(kind="channel_open", anchor=bc.anchor, ticks=30, unit=1, expires=led.height + 500)
        wallets.sign_tx(op, pm.wallet, led)
        opening = PaymentPayload(x402_version=2, accepted=creq, payload={"open": op, "tick": 7, "word": bc.words[7]})
        ov = fac.verify(opening, creq)
        os1 = fac.settle(opening, creq)
        os2 = fac.settle(opening, creq)
        nxt = PaymentPayload(x402_version=2, accepted=creq, payload={"channel": op["txid"], "tick": 14, "word": bc.words[14]})
        os3 = fac.settle(nxt, creq)
        out["facilitator_opening"] = {"verify": ov.is_valid, "settle": [os1.success, os1.payer == pm.wallet.address],
                                      "settle_again": [os2.success, os2.error_reason], "next_word": os3.success}
        print(json.dumps(out, indent=1))
        ok = (out["facilitator_opening"] == {"verify": True, "settle": [True, True],
                                             "settle_again": [False, "invalid_paymesh_channel_payment"], "next_word": True}
              and out["facilitator"]["verify"] == [True, True] and out["facilitator"]["settle"] == [True, True]
              and out["facilitator"]["settle_again"][0] is False
              and out["paid_by_reference_client"]["status"] == 200 and out["paid_by_reference_client"]["success"]
              and out["balance_after"] == 94 and  # 100 - 5 - the 1 PMX fee
              out["channel_via_reference_encoder"]["status"] == 200
              and out["channel_replay"]["status"] == 402)
        print("REFERENCE INTEROP OK" if ok else "REFERENCE INTEROP FAILED")
        return 0 if ok else 1
    finally:
        node.kill()
        node.wait()
        import shutil
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
