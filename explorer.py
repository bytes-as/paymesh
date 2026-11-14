"""paymesh explorer: HTML dashboard on localhost:8403 — chain, txs, balances.

Auto-refreshes every 2s. Stdlib only; run:
    py -3 explorer.py
"""
import http.server
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chain
import wallets

HOST, PORT = "127.0.0.1", int(os.environ.get("PAYMESH_EXPLORER_PORT", 8403))
chain_inst: chain.Chain = None
PRICES_MAP: dict = {}

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>paymesh explorer</title>
<meta http-equiv="refresh" content="2">
<style>
body{font-family:Consolas,monospace;background:#0d1117;color:#c9d1d9;margin:24px}
h1{color:#58a6ff} h2{color:#8b949e;border-bottom:1px solid #30363d;padding-bottom:4px}
table{border-collapse:collapse;margin-bottom:24px} td,th{border:1px solid #30363d;padding:4px 10px;text-align:left}
th{background:#161b22;color:#58a6ff} .ok{color:#3fb950}
code{color:#ffa657}
</style></head><body>
<h1>paymesh explorer</h1>
<h2>chain</h2>
<table id="chain"><tr><th>block</th><th>hash</th><th>prev</th><th>nonce</th><th>txs</th></tr></table>
<h2>transactions</h2>
<table id="txs"><tr><th>txid</th><th>from</th><th>to</th><th>amount</th><th>memo</th></tr></table>
<h2>endpoint prices</h2>
<table id="prices"><tr><th>endpoint</th><th>price PMX</th></tr></table>
<h2>wallet balances</h2>
<table id="bal"><tr><th>wallet</th><th>address</th><th>balance PMX</th></tr></table>
<script>
const data = DATA;
const ESC = {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
const esc = s => String(s).replace(/[&<>"']/g, ch => ESC[ch]);
const c = document.getElementById('chain');
[...data.blocks].reverse().forEach(b=>{
  c.insertAdjacentHTML('beforeend', `<tr><td>${b.index}</td><td><code>${b.hash.slice(0,16)}..</code></td><td><code>${b.prev_hash.slice(0,10)}..</code></td><td>${b.nonce}</td><td>${b.txs.length}</td></tr>`)});
const t = document.getElementById('txs');
[...data.txs].reverse().forEach(x=>{
  t.insertAdjacentHTML('beforeend', `<tr><td><code>${x.txid.slice(0,16)}..</code></td><td><code>${x.from_wallet.slice(0,10)}..</code></td><td><code>${x.to_wallet.slice(0,10)}..</code></td><td>${esc(x.amount)}</td><td>${esc(x.memo)}</td></tr>`)});
const p = document.getElementById('prices');
Object.entries(data.prices||{}).forEach(([r,v])=>{
  p.insertAdjacentHTML('beforeend', `<tr><td><code>${esc(r)}</code></td><td class="ok">${esc(v)}</td></tr>`)});
const b = document.getElementById('bal');
Object.entries(data.balances).forEach(([a,v])=>{
  b.insertAdjacentHTML('beforeend', `<tr><td>${esc(data.names[a]||'?')}</td><td><code>${a.slice(0,16)}..</code></td><td class="ok">${v}</td></tr>`)});
</script>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, "ok")
        else:
            with chain_inst.lock:
                blocks = [b.to_dict() for b in chain_inst.blocks]
            data = {"blocks": [dict(b, txs=[_slim(t) for t in b["txs"]]) for b in blocks],
                    "txs": [_slim(t) for t in chain_inst.all_txs()],
                    "balances": chain_inst.balances(),
                    "prices": PRICES_MAP,
                    "names": {w.address: w.name for w in _named_wallets()}}
            # "<" escaped so no value (a memo) can close the <script> it sits in
            self._send(200, PAGE.replace("DATA", json.dumps(data).replace("<", "\\u003c")))

    def _send(self, code, body):
        body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _slim(tx: dict) -> dict:
    """A tx without its 50 KB Lamport pub/sig, for display."""
    return {k: v for k, v in tx.items() if k not in ("pub", "sig")}


def _named_wallets():
    os.makedirs(wallets.WALLET_DIR, exist_ok=True)
    out = []
    for f in os.listdir(wallets.WALLET_DIR):
        if f.endswith(".wallet.json"):
            try:
                out.append(wallets.load(f[:-len(".wallet.json")]))
            except Exception:
                pass
    return out


def start(chain_: chain.Chain, port=PORT, prices: dict | None = None) -> http.server.ThreadingHTTPServer:
    global chain_inst, PRICES_MAP
    chain_inst = chain_
    PRICES_MAP = prices or {}
    httpd = http.server.ThreadingHTTPServer((HOST, port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"[explorer] http://{HOST}:{port} (auto-refresh 2s)")
    return httpd


if __name__ == "__main__":
    c = chain.Chain()
    if not c.blocks:
        c.genesis()
    start(c)
    while True:
        threading.Event().wait(3600)
