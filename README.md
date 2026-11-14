# paymesh

x402-style **pay-per-call micropayment settlement** on a local Python mini-blockchain. Any HTTP API can drop the `paywall` middleware onto its handler and start charging PMX per call; autonomous agents discover the `402` challenge, pay on-chain, and retry — no accounts, no keys on the wire, no payment processor.

**Why it's interesting:** agents need to pay APIs by the call, in amounts too small for cards. Early niche: agent-to-API micropayments with settlement you can run and verify yourself.

## Run the system locally (one command)

```
cd C:\Users\ARUN\OpenBot\Shared\paymesh
py -3 paymesh.py            # run the whole system in the foreground (Ctrl-C stops)
py -3 paymesh.py start      # or detached in the background (logs: paymesh.log)
py -3 paymesh.py status     # is it up?
py -3 paymesh.py stop       # stop the detached system
```

What runs: the mini blockchain (`chain.json`), the paid API on :8402 and the
explorer on :8403, all in one process — the server is the sole miner.

The demo is the flows running locally against the live system:

```
py -3 buyer_agent.py                    # agent discovers 402s, pays 5 PMX for
                                        # /premium/price and 20 PMX for
                                        # /premium/history, auto-funds from /faucet
open http://127.0.0.1:8403              # watch chain, txs, balances live
```

Free routes need no payment: `GET /open/info`, `GET /faucet?name=you`
(mints 100 PMX), `GET /health`. Agents submit signed txs with
`POST /tx` — the server verifies the Lamport signature and mines.

## Architecture

```
 buyer_agent.py            server.py (:8402)          chain.py
 (autonomous)              GET /premium/price         mini blockchain
      |                         |                      sha256-linked blocks
      | 1. GET ---------------> |                      PoW difficulty 2
      | <-- 402 + challenge --- |  (paywall.py)         chain.json
      |    nonce, recipient,    |------ open_challenge (nonce = tx memo)
      |    amount               |
      | 2. sign: build tx       |  wallets.py
      |    memo=nonce, Lamport- |  Lamport one-time sig, pure hashlib
      |    sign (from,to,amount,|  address = fingerprint of the public key
      |    memo,txid)           |
      |    POST /tx ------------+--------> verify_tx(): wrong/no key rejected;
      | 3. GET + X-Payment: txid              server is the sole miner
      | -----------------------> verify tx in block, nonce unspent
      | <-- 200 price data -----        (replay / double-pay / forgery rejected)

Signature flow: each tx carries pub (512 sha256 hashes) + sig (256 revealed
secrets, one per digest bit). Chain rule: pub must fingerprint to from_wallet
and the Lamport verify must pass; only the zero-address faucet may send
unsigned txs. So a client cannot spend another wallet's address by naming it.

 explorer.py (:8403) — chain, txs, balances, auto-refresh 2s
```

## Attack guarantees

- **Replay:** each challenge nonce is consumed on first verify; reusing a spent `X-Payment` header returns 402.
- **Double-pay:** a nonce binds to exact recipient+amount+path via its memo; a second verify of the same txid finds the challenge already spent.
- **Broke agent:** client checks balance before paying; a 0-balance wallet can't fund the tx.

## Files

| file | role |
|---|---|
| `chain.py` | mini blockchain, PoW, `chain.json` persistence |
| `wallets.py` | Lamport one-time signatures (pure hashlib), chain-scan balances |
| `paywall.py` | 402 challenge/verify middleware |
| `paymesh.py` | system launcher: run/start/stop/status |
| `server.py` | demo paid API with a per-endpoint price table (`/open/info` 0, `/premium/price` 5, `/premium/history` 20, free `/faucet`, `POST /tx`) on :8402 |
| `buyer_agent.py` | agent that discovers 402, self-pays, retries (price + history) |
| `explorer.py` | HTML dashboard on :8403 |

Python 3 stdlib only, no dependencies.
