"""The quickstart's Python step: pay the node and the weather seller every way paymesh offers.

    py -3 paymesh.py start                 # the node: chain, paid API :8402, explorer :8403
    py -3 examples/weather_api.py          # a second seller on :8404 (another terminal)
    py -3 examples/quickstart.py

Ports in use? Set PAYMESH_API_PORT / PAYMESH_EXPLORER_PORT / WEATHER_PORT for all three.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from paymesh_client import Client  # noqa: E402

NODE = os.environ.get("PAYMESH_API", f"http://127.0.0.1:{os.environ.get('PAYMESH_API_PORT', 8402)}")
SHOP = f"http://127.0.0.1:{os.environ.get('WEATHER_PORT', 8404)}"


def main():
    api = Client(NODE, wallet="quickstart")
    api.faucet()
    api.faucet()
    print(f"wallet {api.wallet.address[:16]}...  balance {api.balance()} PMX (test coins from the faucet)")

    status, data = api.get("/premium/price")  # 402 -> a signed tx in PAYMENT-SIGNATURE -> 200
    r = api.last_receipt()
    print(f"1. on-chain:  {status} SYNTH/USD {data['price']}  tx {r['transaction'][:16]}...")

    api.open_channel(ticks=50)  # one deposit with this seller, then a hash per call
    for _ in range(3):
        status, data = api.get("/premium/price")
    print(f"2. channel:   {status} SYNTH/USD {data['price']}  (3 calls, no tx each)")
    print(f"              closed: {api.close_channel()['paid_to_seller']} PMX to the seller, the rest back")

    api.open_hub(ticks=60)  # one deposit with the node's hub pays any seller on it
    status, data = api.get("/premium/price")
    print(f"3. hub:       {status} SYNTH/USD {data['price']}  (node)")
    try:
        status, data = api.get(f"{SHOP}/forecast")
        print(f"              {status} {data}  (weather seller, same deposit)")
    except OSError:
        print(f"              (no weather seller at {SHOP}: start examples/weather_api.py to pay a second seller)")
    api.close_channel()  # one tx pays every seller its share
    print(f"done: balance {api.balance()} PMX. Explorer: http://127.0.0.1:{os.environ.get('PAYMESH_EXPLORER_PORT', 8403)}")
    api.close()


if __name__ == "__main__":
    main()
