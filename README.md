# paymesh

x402-style **pay-per-call micropayments** for HTTP APIs, settled on a local
Python mini-blockchain. A paid route answers `402 Payment Required`; an agent
pays and retries, with no accounts and no payment processor. Two ways to pay:

* **on-chain**: one signed tx per call (about 7 ms on this machine);
* **payment channel**: one deposit on-chain, then every call carries the next
  word of a hash chain, checked with a few hashes (0.4 ms), settled in one block.

Python 3 stdlib only. Protocol details: [PROTOCOL.md](PROTOCOL.md).

## Run it

```
cd C:\Users\ARUN\OpenBot\Shared\paymesh
py -3 paymesh.py start        # chain + paid API on :8402 + explorer on :8403 (background)
py -3 buyer_agent.py          # demo: 2 calls paid on-chain, 20 through a channel, settle
open http://127.0.0.1:8403    # explorer: stats, channels, blocks, txs, balances
py -3 paymesh.py status | stop
```

## Use it from an agent

```python
from paymesh_client import Client
api = Client("http://127.0.0.1:8402", wallet="my-agent")
api.faucet()                              # demo money: 100 PMX
status, data = api.get("/premium/price")  # pays on-chain by itself
api.open_channel(ticks=500)               # lock 500 PMX once
status, data = api.get("/premium/price")  # now paid from the channel
api.close_channel()                       # settle; the unspent rest comes back
```

The client learns each route's price from its first 402, keeps one HTTP
connection alive, and saves its channel so it can resume after a restart.

## Numbers

`py -3 bench.py 100 2000`, on a separate server (never the live one), this machine:

| | on-chain | channel | 8 agents, channels |
|---|---|---|---|
| paid calls per second | 142 | 2,467 | 3,320 (0 errors) |
| latency p50 / p95 | 6.3 / 16.1 ms | 0.39 / 0.49 ms | |
| chain bytes per call | 5,238 | 5 | |

A paid tx costs 7.7 ms whether the chain has 100 or 2,000 blocks (blocks are
appended, not rewritten).

## Guarantees, each covered by a test

`py -3 -m unittest discover -s tests` (31 tests: attacks, channels, HTTP end
to end, client SDK).

* **Keys are one-time and rotate every tx.** A signature can't be reused or
  extended into a forgery; a spent key never verifies again.
* **No overspending, negative or zero amounts, or minting.** Pending txs count
  against the balance; only `GET /faucet` mints, at most 100 PMX.
* **A signed tx counts once.** Resubmitting it is refused.
* **A payment pays once.** Replayed `X-Payment` headers and spent channel words
  are refused, also after a server restart. A wrong payment can't burn someone
  else's challenge.
* **Channel money is safe.** The seller can only claim ticks the buyer
  revealed; signed channel terms can't be altered; an unsettled channel
  refunds to the buyer after expiry, and the server settles paid channels
  before that.
* **The chain file is checked on load.** Every block is replayed through the
  same rules, so an edited history is refused; a block cut off by a crash is
  dropped cleanly.
* **The HTTP surface holds.** Wallet names can't escape `wallets/`, bodies
  are size-capped, bad input gets `400` (never a crash), and the explorer
  escapes everything it shows.

## Limits (it's a proof of concept)

* PMX is play money: the faucet mints it for anyone, and one server is the
  only miner, so there's no consensus and no fees.
* Wallet files hold their seed in plain text under `wallets/` (kept out of git).
* The chain is verified in full on every start (about 0.45 s per 1,000 blocks).
* The payment format is x402-style, not wire-compatible with the x402 spec.

## Files

| file | role |
|---|---|
| `chain.py` | ledger rules, blocks, PoW, append-only `chain.json` |
| `wallets.py` | one-time keys (WOTS, Lamport for old keys), rotation, signing |
| `channels.py` | PayWord payment channels: buyer hash chain, seller log |
| `paywall.py` | 402 challenges and on-chain payment checks |
| `server.py` | demo paid API: prices, `/tx`, `/account`, `/faucet`, channel routes |
| `paymesh_client.py` | client SDK |
| `buyer_agent.py` | demo agent |
| `explorer.py` | dashboard on :8403 |
| `paymesh.py` | launcher: run / start / stop / status |
| `bench.py` | throughput benchmark |
| `tests/` | the test suite |
