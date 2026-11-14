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
<style>
body{font-family:Consolas,monospace;background:#0d1117;color:#c9d1d9;margin:24px}
h1{color:#58a6ff} h2{color:#8b949e;border-bottom:1px solid #30363d;padding-bottom:4px}
table{border-collapse:collapse;margin-bottom:24px} td,th{border:1px solid #30363d;padding:4px 10px;text-align:left}
th{background:#161b22;color:#58a6ff} .ok{color:#3fb950} .dim{color:#8b949e}
code{color:#ffa657}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin:8px 0 24px}
.stat{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px 14px;min-width:120px}
.stat b{display:block;font-size:22px;color:#e6edf3}.stat span{color:#8b949e;font-size:12px}
</style></head><body>
<h1>paymesh explorer</h1>
<div class="stats" id="stats"></div>
<h2>seller revenue <span class="dim">(since the API started)</span></h2>
<table id="rev"><tr><th>route</th><th>paid calls</th><th>on-chain</th><th>channel</th><th>revenue PMX</th></tr></table>
<h2>payment channels</h2>
<table id="chans"><tr><th>channel</th><th>buyer</th><th>seller</th><th>deposit PMX</th><th>paid PMX</th><th>expires at block</th><th>state</th></tr></table>
<h2>chain <span class="dim">(last 50 blocks)</span></h2>
<table id="chain"><tr><th>block</th><th>hash</th><th>prev</th><th>nonce</th><th>txs</th></tr></table>
<h2>transactions <span class="dim">(last 50)</span></h2>
<table id="txs"><tr><th>txid</th><th>kind</th><th>from</th><th>to</th><th>amount</th><th>memo</th></tr></table>
<h2>endpoint prices</h2>
<table id="prices"><tr><th>endpoint</th><th>price PMX</th></tr></table>
<h2>wallet balances</h2>
<table id="bal"><tr><th>wallet</th><th>address</th><th>balance PMX</th></tr></table>
<script>
const initial = DATA;
const ESC = {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
const esc = s => String(s).replace(/[&<>"']/g, ch => ESC[ch]);
function clear(id){[...document.querySelectorAll('#'+id+' tr')].slice(1).forEach(r=>r.remove())}  // keep the header row
function render(data){
['rev','chans','chain','txs','prices','bal'].forEach(clear);document.getElementById('stats').innerHTML='';
const st = document.getElementById('stats');
data.stats.forEach(([k,v])=>st.insertAdjacentHTML('beforeend', `<div class="stat"><b>${esc(v)}</b><span>${esc(k)}</span></div>`));
const rv = document.getElementById('rev');
const rs = data.revenue && data.revenue.routes || {};
Object.entries(rs).forEach(([p,r])=>rv.insertAdjacentHTML('beforeend', `<tr><td><code>${esc(p)}</code></td><td>${esc(r.calls)}</td><td>${esc(r.on_chain)}</td><td>${esc(r.channel)}</td><td class="ok">${esc(r.revenue)}</td></tr>`));
if(!Object.keys(rs).length) rv.insertAdjacentHTML('beforeend','<tr><td colspan="5" class="dim">no paid calls yet</td></tr>');
const ch = document.getElementById('chans');
data.channels.forEach(x=>ch.insertAdjacentHTML('beforeend', `<tr><td><code>${esc(x.id.slice(0,12))}..</code></td><td>${esc(x.buyer_name)}</td><td>${esc(x.seller_name)}</td><td>${esc(x.deposit)}</td><td class="ok">${esc(x.paid)}</td><td>${esc(x.expires)}</td><td>${x.open?'<span class="ok">open</span>':'settled'}</td></tr>`));
if(!data.channels.length) ch.insertAdjacentHTML('beforeend','<tr><td colspan="7" class="dim">no channels yet</td></tr>');
const c = document.getElementById('chain');
[...data.blocks].reverse().forEach(b=>{
  c.insertAdjacentHTML('beforeend', `<tr><td>${b.index}</td><td><code>${b.hash.slice(0,16)}..</code></td><td><code>${b.prev_hash.slice(0,10)}..</code></td><td>${b.nonce}</td><td>${b.txs.length}</td></tr>`)});
const t = document.getElementById('txs');
[...data.txs].reverse().forEach(x=>{
  t.insertAdjacentHTML('beforeend', `<tr><td><code>${x.txid.slice(0,16)}..</code></td><td>${esc(x.kind||'pay')}</td><td><code>${x.from_wallet.slice(0,10)}..</code></td><td><code>${x.to_wallet.slice(0,10)}..</code></td><td>${esc(x.amount)}</td><td>${esc(x.memo)}</td></tr>`)});
const p = document.getElementById('prices');
Object.entries(data.prices||{}).forEach(([r,v])=>{
  p.insertAdjacentHTML('beforeend', `<tr><td><code>${esc(r)}</code></td><td class="ok">${esc(v)}</td></tr>`)});
const b = document.getElementById('bal');
Object.entries(data.balances).forEach(([a,v])=>{
  b.insertAdjacentHTML('beforeend', `<tr><td>${esc(data.names[a]||'?')}</td><td><code>${a.slice(0,16)}..</code></td><td class="ok">${v}</td></tr>`)});
}
render(initial);
// live: fetch the data every 2 s and redraw the tables (no page reload, scroll kept)
setInterval(()=>fetch('/data.json').then(r=>r.json()).then(render).catch(()=>{}),2000);
</script>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send(200, "ok")
        else:
            names = {w.address: w.name for w in _named_wallets()}
            with chain_inst.lock:
                blocks = [b.to_dict() for b in chain_inst.blocks[-50:]]
                txs = [_slim(t) for b in chain_inst.blocks[-200:] for t in b.txs][-50:]
                n_txs = sum(len(b.txs) for b in chain_inst.blocks)
                chans = [dict(v, id=k) for k, v in chain_inst.ledger.channels.items()]
                height = chain_inst.height
            nm = lambda a: names.get(a, a[:10] + "..")  # noqa: E731
            channels_view = [{"id": c["id"], "buyer_name": nm(c["buyer"]), "seller_name": nm(c["seller"]),
                              "deposit": c["n"] * c["unit"], "paid": c.get("paid", "-") if (not c["open"] or c.get("paid_ticks")) else "-",  # payouts: paid so far
                              "expires": c["expires"], "open": c["open"]} for c in reversed(chans[-50:])]
            stats = [("blocks", height), ("transactions", n_txs),
                     ("open channels", sum(c["open"] for c in chans)),
                     ("PMX locked in channels", sum((c["n"] - c.get("paid_ticks", 0)) * c["unit"] for c in chans if c["open"])),
                     ("PMX settled via channels", sum(c.get("paid", 0) for c in chans if not c["open"] or c.get("paid_ticks")))]
            data = {"blocks": [dict(b, txs=[_slim(t) for t in b["txs"]]) for b in blocks],
                    "txs": txs, "stats": stats, "channels": channels_view,
                    "balances": chain_inst.balances(),
                    "prices": PRICES_MAP,
                    "names": names, "revenue": _revenue()}
            if self.path.startswith("/data.json"):
                self._send(200, json.dumps(data), "application/json")
                return
            # "<" escaped so no value (a memo) can close the <script> it sits in
            self._send(200, PAGE.replace("DATA", json.dumps(data).replace("<", "\\u003c")))

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _revenue() -> dict:
    """The API's /stats, when it runs in this process (paymesh.py serves both)."""
    srv = sys.modules.get("server")
    return srv.STATS.view() if srv is not None and hasattr(srv, "STATS") else {}


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
