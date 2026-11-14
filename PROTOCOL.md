# paymesh protocol

Everything a client or server needs to interoperate with paymesh. Amounts are
whole PMX. Hashes are SHA-256, written as 64 lowercase hex characters.

## 1. Paying for a call

### 1a. On-chain (one tx per call)

1. `GET /premium/price` without payment →

   ```
   HTTP/1.1 402 Payment Required
   {"error": "payment required", "recipient": "<seller address>", "amount": 5,
    "currency": "PMX", "memo": "<recipient>:<nonce>:<amount>", "nonce": "<32 hex>",
    "expires_in": 300, "chain_tip": "<block hash>",
    "channel": {...channel terms, see 1b...}}
   ```

2. The client signs a `pay` tx (section 3) to `recipient` for `amount` with
   `memo` set to the challenge memo, and `POST /tx`s it. The server mines it at
   once and answers `{"accepted": true, "txid": ..., "block": n}`.
3. The client repeats the request with `X-Payment: <txid>`.

The server accepts the payment only when the tx is mined, its memo equals the
open challenge's memo exactly, it pays the quoted recipient and amount, the
path matches, and the challenge hasn't expired. Only then is the challenge
spent; a txid pays once. Challenges live 300 s; at most 10,000 are open.

### 1b. Payment channel (one deposit, then a hash per call)

1. `GET /channel/terms` → `{"seller", "unit": 1, "currency", "max_ticks", ...}`.
2. The buyer picks `ticks`, draws a random 32-byte secret `w_ticks`, and
   computes `w_(k-1) = sha256(w_k)` down to the anchor `w_0`. It `POST /tx`s a
   signed `channel_open` tx locking `ticks * unit` PMX (section 3).
3. Each paid call carries

   ```
   X-Channel: <channel id = channel_open txid> <i> <w_i>
   ```

   paying `(i - last) * unit` PMX, where `last` is the highest tick the seller
   has accepted. The seller checks `sha256^(i-last)(w_i) == w_last` and that
   the amount covers the route's price, appends the tick to its log, then serves.
   A refused channel payment answers `402 {"error": "channel payment refused", "reason"}`.
4. `GET /channel/state?channel=<id>` → `{"paid_ticks", "ticks", "unit", "open", "expires"}`:
   a restarted buyer resumes from `paid_ticks`.
5. `POST /channel/close {"channel": "<id>"}` (anyone may ask) makes the seller
   settle on-chain with its best `(i, w_i)`: `i * unit` PMX to the seller, the
   rest back to the buyer. The server also settles paid channels by itself 20
   blocks before they expire. An unsettled channel can be refunded in full by
   the buyer from its `expires` height on.

## 2. Other endpoints

| method and path | answer |
|---|---|
| `GET /health` | `{"status": "ok", "chain_blocks": n}` |
| `GET /open/info` | free route: the price table |
| `GET /account?address=<addr>` | `{"balance", "current_key", "height", "channels"}`: what a client needs to sign its next tx |
| `GET /faucet?address=<addr>` | mints 100 PMX to `addr` (demo only); `?name=` creates a wallet on the server's machine |
| `POST /tx` | submit a signed tx (body up to 200 KB); `400 {"error"}` if any rule fails. Faucet txs are refused here. |
| `GET /stats` | paid calls and PMX per route, on-chain / channel, distinct payers |
| any of `/faucet`, `/tx`, a 402 challenge | `429 {"error": "too many requests", "retry_after_s"}` with `Retry-After` when a client is over its limit |

The server speaks HTTP/1.1 with keep-alive. It binds 127.0.0.1 by default
(`PAYMESH_API_PORT`, `PAYMESH_EXPLORER_PORT`, `PAYMESH_HOME` change ports and
the data folder).

## 3. Transactions

```json
{"from_wallet": "<addr>", "to_wallet": "<addr>", "amount": 5, "memo": "text <= 256",
 "txid": "<64 hex, unique>", "next_key": "<fingerprint of the sender's next key>",
 "kind": "pay | channel_open | channel_close | channel_refund",
 "sig": ["<64 hex>", ...], "pub": [...Lamport only...],
 "anchor", "ticks", "unit", "expires"   // channel_open
 "channel", "ticks", "word"             // channel_close / channel_refund (channel only)
}
```

`kind` defaults to `pay`. Every rule is enforced by the ledger, for new txs and
when `chain.json` is loaded:

* addresses and txid are 64 lowercase hex; `amount` is a whole number above 0;
* a txid is used once;
* `pay`: signed by the sender's current key; the sender holds the amount (pending txs counted);
* `channel_open`: `amount == ticks * unit`, `1 <= ticks <= 1,000,000`, `expires` in the future, funds held;
* `channel_close`: from the channel's seller to its buyer, `amount == ticks * unit`, `sha256^ticks(word) == anchor`, signed by the seller;
* `channel_refund`: from the buyer to itself for the whole deposit, from the `expires` height on;
* the faucet (address `0` x 64) mints at most 100 PMX per tx, only through `GET /faucet`.
* a tx carries only its kind's fields (above); anything else is refused.

## 4. Signatures and keys

Each wallet is a chain of **one-time keys** derived from a 32-byte seed. The
wallet address is the fingerprint of key 0. Every signed tx names the
fingerprint of the sender's next key in `next_key`; the ledger accepts the
sender's next tx only from that key. A key never verifies twice, so the half
of a one-time key that a signature publishes is useless to anyone else.

* **WOTS (w=16)**, used for all new keys: the message digest is
  `sha256(from|to|amount|memo|txid|next_key[|json(kind, anchor, ticks, unit, expires, channel, word)])`.
  Its 64 base-16 digits plus a 3-digit checksum (`sum(15 - d)`) pick how far
  each of 67 hash chains is walked. `sig[j] = sha256^(d_j)(sk_j)`. The verifier
  finishes each chain (`15 - d_j` more steps) and compares
  `sha256("wots1" | pk_0 | ... | pk_66)` to the expected key fingerprint.
  A signed tx is about 4.9 KB.
* **Lamport** (older wallets' last key): 256 pairs of secrets; `pub` (512
  hashes) travels with the tx; fingerprint `sha256(concat(pub hex))`. About 52 KB.

## 5. Chain file

`chain.json` is a `{"format": 3}` line followed by one block per line
(append-only). A block is `{"index", "timestamp", "txs", "prev_hash", "nonce",
"hash"}`; its hash is `sha256("index|timestamp|json(txs, sorted keys)|prev_hash|nonce")`
and starts with `00` (proof of work, difficulty 2). The server is the only
miner; a miner thread puts every pending tx into its next block, and `POST /tx`
answers once the tx is mined. Every 500 blocks the ledger is checkpointed to
`chain.json.ckpt` (tip hash + ledger) to make restarts fast.
