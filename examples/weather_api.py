"""An API that isn't paymesh, charging with paymesh: /forecast costs 3 PMX.

    py -3 paymesh.py start                     # the node (chain, faucet) on :8402
    py -3 examples/weather_api.py              # this seller on :8404

    from paymesh_client import Client
    api = Client("http://127.0.0.1:8404", wallet="me", node="http://127.0.0.1:8402")
    api.faucet(); api.get("/forecast")         # pays 3 PMX to the weather seller
"""
import json
import os
import random
import socketserver
import sys
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from paymesh_seller import Paywall  # noqa: E402


def weather_app(environ, start_response):
    """The seller's own app: knows nothing about payments."""
    if environ["PATH_INFO"] == "/forecast":
        body = {"city": "Bengaluru", "tomorrow_c": round(22 + random.random() * 8, 1), "rain_pct": random.randint(0, 100)}
    elif environ["PATH_INFO"] == "/":
        body = {"service": "weather", "paid_routes": {"/forecast": 3}}
    else:
        start_response("404 Not Found", [("Content-Type", "application/json")])
        return [b'{"error": "not found"}']
    data = json.dumps(body).encode()
    start_response("200 OK", [("Content-Type", "application/json"), ("Content-Length", str(len(data)))])
    return [data]


class NoDelayHandler(WSGIRequestHandler):
    disable_nagle_algorithm = True  # TCP_NODELAY: no ~40 ms delayed-ACK stall on Linux


class ThreadingWSGIServer(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True


def build(node: str, wallet: str = "weather-seller"):
    paywall = Paywall(node=node, wallet=wallet, prices={"/forecast": 3})
    return paywall, paywall.wsgi(weather_app)


if __name__ == "__main__":
    node = os.environ.get("PAYMESH_API") or f"http://127.0.0.1:{os.environ.get('PAYMESH_API_PORT', 8402)}"
    port = int(os.environ.get("WEATHER_PORT", 8404))
    _, app = build(node)
    httpd = make_server("127.0.0.1", port, app, server_class=ThreadingWSGIServer, handler_class=NoDelayHandler)
    print(f"[weather] http://127.0.0.1:{port}/forecast costs 3 PMX (node {node})")
    httpd.serve_forever()
