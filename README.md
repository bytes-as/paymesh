# paymesh

**x402 v2** pay-per-call micropayments for HTTP APIs, settled on a local Python
mini-blockchain. A paid route answers `402 Payment Required` with a
`PAYMENT-REQUIRED` header; an agent pays in `PAYMENT-SIGNATURE` and gets a
`PAYMENT-RESPONSE` receipt, with no accounts and no payment processor. paymesh
is an x402 scheme (`paymesh`) with two ways to pay:

* **on-chain**: one signed tx per call, sent in the header and settled by the seller;
* **payment channel**: one deposit on-chain, then every call carries the next
  word of a hash chain, checked with a few hashes (0.4 ms), settled in one block.

Python 3 stdlib only. Protocol details: [PROTOCOL.md](PROTOCOL.md). Checked against the
reference x402 Python SDK: `interop/x402_reference.py`.

## Quickstart (5 minutes)

You need Python 3.10 or newer and nothing else: no packages, no accounts, no real money.
Commands use `py -3` (Windows); on macOS and Linux write `python3` instead.

**1. Get it and start a node** (a local chain, a paid API on :8402, an explorer on :8403):

```
git clone https://github.com/bytes-as/paymesh
cd paymesh
py -3 paymesh.py start
```

Open http://127.0.0.1:8403 to watch blocks, payments and channels arrive.

**2. Start a second seller** in another terminal, in the same folder: an API that runs no
paymesh code of its own and charges 3 PMX per `/forecast` through the node.

```
py -3 examples/weather_api.py
```

**3. Pay from Python**: one call paid on-chain, three through a payment channel, then one
deposit with the node's hub paying both sellers:

```
py -3 examples/quickstart.py
```

**4. Pay from a browser**: serve the demo page on another origin and open it:

```
py -3 -m http.server 8410 --directory js
```

Open http://localhost:8410/demo.html, copy the wallet address the page shows, and fund it
with test coins from a third terminal:

```
py -3 wallet.py faucet <the address>
```

Then click the blue buttons (a tx, a channel, the hub paying both sellers). The red ones
show that a page on another site can't mint coins or read accounts. Tested in Edge; any
current browser should do (Firefox: open the same URL).

**5. Stop**: Ctrl+C the weather seller and the page server, then `py -3 paymesh.py stop`.

Ports taken? Set `PAYMESH_API_PORT`, `PAYMESH_EXPLORER_PORT` and `WEATHER_PORT` in each
terminal, and open the page as `demo.html?api=http://127.0.0.1:<api port>&shop=http://127.0.0.1:<weather port>`.
The chain and wallets live in this folder (`chain.json`, `wallets/`); delete them to start over.

More: `py -3 buyer_agent.py` (an agent that discovers prices, pays and logs each payment),
`py -3 examples/payout_demo.py` (sellers paid on schedule), `py -3 paymesh.py status`.

## Use it from an agent

```python
from paymesh_client import Client
api = Client("http://127.0.0.1:8402", wallet="my-agent")
api.faucet()                              # demo money: 100 PMX
status, data = api.get("/premium/price")  # pays on-chain by itself
api.last_receipt()                        # the PAYMENT-RESPONSE: success, txid, payer
api.open_channel(ticks=500)               # lock 500 PMX once
status, data = api.get("/premium/price")  # now paid from the channel
api.close_channel()                       # settle; the unspent rest comes back

api.open_hub(ticks=500)                   # or: one deposit with the node's hub...
api.get("/premium/price")                 # ...pays this API
api.get("http://127.0.0.1:8404/forecast") # ...and any other seller on that node
api.close_channel()                       # one tx pays every seller its share
```

The client learns each route's price from its first 402, keeps one HTTP
connection alive, and saves its channel so it can resume after a restart.

### From a web page (`js/paymesh.js`)

One dependency-free JavaScript file pays an x402 route from a browser (or Node.js). It
signs WOTS v2 payments byte for byte as `wallets.py` does (`tests/test_js_client.py`
compares them with Node.js):

```html
<script src="paymesh.js"></script>
<script>
  const wallet = Paymesh.Wallet.create();   // fund wallet.address from a script or the CLI
  const api = new Paymesh.Client("http://127.0.0.1:8402", wallet, w => localStorage.setItem("w", JSON.stringify(w)));
  api.get("/premium/price").then(r => console.log(r.status, r.body, r.receipt));
</script>
```

`js/demo.html` does this with buttons: serve the folder on another origin
(`py -3 -m http.server 8410 --directory js`, open `http://localhost:8410/demo.html`).
A page can pay, but it can't mint coins, post txs or read `/account` (PROTOCOL 0c). So the
wallet tracks its own key index, and it locks if a payment is refused after signing,
instead of signing again with a one-time key a seller has seen.

With `new Paymesh.Client(api, wallet, save, { channelTicks: 100 })`, the first paid call to a
seller opens a channel inside its payment. A page can't post to `/tx`, so the signed
`channel_open` travels in `PAYMENT-SIGNATURE` and the seller mines it. Later calls pay with
the next word of the hash chain: no transaction and no new key. Add `hub: true` and the
channel goes to the seller's hub instead, so one deposit pays every seller that takes hub
channels (the node and sellers running apart).

## Charge for your own API

```python
from paymesh_seller import Paywall
paywall = Paywall(node="http://127.0.0.1:8402", wallet="my-shop", prices={"/forecast": 3})
app = paywall.wsgi(my_wsgi_app)   # or, in any framework: ok, status, body, headers = paywall.gate(path, headers, url)
```

The seller runs apart from the node: it keeps its own wallet, challenges and
channel log, asks the node only to look up mined txs and channels, and
settles channels itself. A buyer points the client at both:
`Client("http://127.0.0.1:8404", wallet="me", node="http://127.0.0.1:8402")`.
Runnable example: `py -3 examples/weather_api.py`.

## Numbers

`py -3 bench.py 100 2000`, on a separate server (never the live one), this machine:

| | on-chain | channel | 8 agents, channels |
|---|---|---|---|
| paid calls per second | 296 | 2,495 | 3,511 (0 errors) |
| latency p50 / p95 | 3.3 / 3.9 ms | 0.38 / 0.48 ms | |
| chain bytes per call | 5,238 | 5 | |
| fee per call | 1 PMX (burned) | 2 PMX per channel, any number of calls | |

With 8 separate client processes paying on-chain the server mines 651 paid
calls per second (pending txs share blocks). The cost of a paid tx doesn't
grow with the chain (blocks are appended), and a restart at 10,000 blocks
takes 0.38 s (ledger checkpoints; a full check runs in the background).

## Guarantees, each covered by a test

`py -3 -m unittest discover -s tests` (132 tests: attacks, channels, HTTP end
to end, client SDK, a separate seller API, x402 headers, crash recovery, seeded fuzzing, wallets at rest).
`py -3 attacks.py` runs 50 attacks against a scratch node and shows each refused.
`py -3 check.py` runs everything (self-tests, suite, attacks, a quick benchmark) in about 10 s.

* **Keys are one-time and rotate every tx.** A signature can't be reused or
  extended into a forgery; a spent key never verifies again. New keys are
  WOTS v2: every hash-chain step is a different hash (per-key public seed,
  chain and step number), so values published by one key are useless under
  another, and old wallets move to v2 at their next signature.
* **No overspending, negative or zero amounts, or minting.** Pending txs count
  against the balance; only `GET /faucet` mints, at most 100 PMX.
* **A signed tx counts once.** Resubmitting it is refused.
* **A payment pays once.** Replayed `PAYMENT-SIGNATURE` / `X-Payment` headers and spent channel words
  are refused, also after a server restart. A wrong payment can't burn someone
  else's challenge. A signed tx in `PAYMENT-SIGNATURE` is put on chain only if it
  pays an open challenge for that route exactly, so a refused one costs nothing.
* **A seller never holds two signatures from one key.** After a seller refuses
  a signed tx, the client retires that key (a self-payment via the node) before
  signing again; the kept tx can no longer be mined. (With a balance of 0 it
  can't, and says so: fund the wallet before paying again.)
* **Chain growth costs.** Every signed tx burns at least 1 PMX per started
  8 KB, the signature covers the fee, and the sender must hold amount + fee;
  PMX is conserved (minted = held + locked + burned).
* **One hub deposit pays many sellers, each word once.** The hub keeps one
  counter per channel: a word spent at one seller is refused at every other,
  and the hub's single close pays each seller exactly what it was credited
  (the ledger checks the splits add up to what the buyer revealed).
* **Channel money is safe.** The seller can only claim ticks the buyer
  revealed; signed channel terms can't be altered; an unsettled channel
  refunds to the buyer after expiry, and the server settles paid channels
  before that. No ticks are taken within 20 blocks of expiry, and a buyer who
  spends a channel then races to refund it loses: a refund opens a 10-minute
  window (by block time, not blocks, which are cheap) in which the seller can
  still close with its best word, so it is paid even if its settler was down
  during the race; the node also settles its own channels first.
* **The chain file is checked on load.** Every block is replayed through the
  same rules, so an edited history is refused.
* **A crash loses nothing that was answered.** A block is on disk (fsync)
  before its tx is answered "mined"; a channel tick is written before the call
  is served, and fsynced within 50 ms (`PAYMESH_FSYNC=strict`: before). A
  record cut off mid-write is cut away on the next start, never read as data;
  a test cuts files at every point of their last record and kills a writer
  process 12 times mid-run, and an attack kills the node and replays a word.
* **The HTTP surface holds.** Wallet names can't escape `wallets/`, bodies
  are size-capped, bad input gets `400` (never a crash: 1,000 fuzzed txs and
  315 fuzzed payment headers per run), a tx can't carry fields beyond its
  kind's, clients over their rate limit get `429`, and the explorer escapes
  everything it shows.
* **Checkpoints can't hide anything.** A checkpoint that doesn't match the
  chain is ignored; one edited to lie is caught by the background full check,
  and the node then refuses txs (`/health` answers 503).

* **Hub sellers are paid on schedule, once.** A hub channel's sellers get
  `channel_payout`s while the channel stays open (every 60 s, or at 50 PMX owed;
  `PAYMESH_HUB_PAYOUT_S` / `_MIN`). The chain records what was paid, so no tick
  is paid twice and close and refund move only the rest. Killing the node before,
  during or after a payout loses nothing and pays nothing twice
  (`tests/test_payout.py`, `tests/test_payout_crash.py`; `tests/test_fuzz_payout.py`
  checks conservation after every step of random runs with crashes).

## Limits (it's a proof of concept)

* PMX is play money: the faucet mints it for anyone, and one server is the
  only miner, so there's no consensus. Every signed tx burns a fee (1 PMX per
  started 8 KB), but while the faucet is free its rate limit, not the fee, is
  what bounds spam.
* Wallet files hold their seed in plain text under `wallets/` (kept out of git)
  unless created with `wallet.py new <name> --encrypt` (scrypt + HMAC-SHA256,
  passphrase from `PAYMESH_PASSPHRASE`).
* Rate limits are per client IP, so clients behind one address share them.
* x402: v2 only, no v1 (`X-PAYMENT`, which here is the native txid header). Paid routes
  are CORS-open for browser clients; other routes refuse other sites (PROTOCOL 0c). The node is
  also an x402 facilitator (`/verify`, `/settle`, `/supported`) for resource servers
  that run no paymesh code.

## Files

| file | role |
|---|---|
| `chain.py` | ledger rules, blocks, PoW, append-only `chain.json` |
| `wallets.py` | one-time keys (WOTS v2; WOTS v1 and Lamport for old keys), rotation, signing |
| `channels.py` | PayWord payment channels: buyer hash chain, seller log |
| `durable.py` | append-only logs, atomic replaces, fsync policy (`PAYMESH_FSYNC`), torn-tail repair |
| `js/paymesh.js`, `js/demo.html` | browser/Node.js client: WOTS v2 signing in JS, x402 payments; a demo page |
| `httppool.py` | kept-alive HTTP connections shared by threads (client SDK, seller middleware) |
| `facilitator.py` | x402 facilitator: `/verify`, `/settle`, `/supported` for the `paymesh` scheme |
| `paymesh_x402.py` | x402 v2 wire format: `PAYMENT-REQUIRED` / `-SIGNATURE` / `-RESPONSE` for the `paymesh` scheme |
| `paywall.py` | 402 challenges and on-chain payment checks |
| `server.py` | demo paid API: prices, `/tx`, `/account`, `/faucet`, `/stats`, channel routes, miner, rate limits |
| `paymesh_client.py` | client SDK |
| `paymesh_seller.py` | seller middleware: price any WSGI app, apart from the node |
| `wallet.py` | wallet command line: new, balance, faucet, send, history, channels |
| `examples/weather_api.py` | a separate API charging 3 PMX per call |
| `buyer_agent.py` | demo agent |
| `explorer.py` | dashboard on :8403 |
| `paymesh.py` | launcher: run / start / stop / status |
| `bench.py` | throughput benchmark |
| `tests/` | the test suite |
| `interop/x402_reference.py` | pays a scratch node with the reference x402 SDK (needs its own venv) |
