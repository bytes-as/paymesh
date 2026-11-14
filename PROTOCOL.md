# paymesh protocol

Everything a client or server needs to interoperate with paymesh. Amounts are
whole PMX. Hashes are SHA-256, written as 64 lowercase hex characters.

## 0. x402 wire format

paymesh speaks **x402 Protocol Version 2** in its HTTP transport, as specified in
[`specs/x402-specification-v2.md`](https://github.com/x402-foundation/x402/blob/31ce6bbf7e3af2f56f07ec9b9ac38168b3ca1a41/specs/x402-specification-v2.md)
and [`specs/transports-v2/http.md`](https://github.com/x402-foundation/x402/blob/31ce6bbf7e3af2f56f07ec9b9ac38168b3ca1a41/specs/transports-v2/http.md)
at commit `31ce6bb` (2026-11-12) of github.com/x402-foundation/x402. Code: `paymesh_x402.py`.

| header | direction | carries (base64 of JSON) |
|---|---|---|
| `PAYMENT-REQUIRED` | server → client, on every 402 | `PaymentRequired` |
| `PAYMENT-SIGNATURE` | client → server, on the retry | `PaymentPayload` |
| `PAYMENT-RESPONSE` | server → client, on the paid 200 or a refused 402/400 | `SettlementResponse` |

Scheme **`paymesh`**, network **`paymesh:<first 32 hex of the genesis block hash>`**
(CAIP-2 shape; `GET /health` reports it), asset `PMX`, `amount` a decimal string of
whole PMX. Every 402 offers two `accepts` entries, one per `extra.assetTransferMethod`;
both settle before the resource runs, so `extra.paymentFlow` is `upfront` (spec 6.1):

```json
{"x402Version": 2, "error": "PAYMENT-SIGNATURE header is required",
 "resource": {"url": "http://127.0.0.1:8402/premium/price", "mimeType": "application/json"},
 "accepts": [
  {"scheme": "paymesh", "network": "paymesh:00f359b2...", "amount": "5", "asset": "PMX",
   "payTo": "<seller address>", "maxTimeoutSeconds": 300,
   "extra": {"assetTransferMethod": "tx", "paymentFlow": "upfront", "memo": "<challenge memo>", "nonce": "<32 hex>"}},
  {"scheme": "paymesh", ..., "extra": {"assetTransferMethod": "channel", "paymentFlow": "upfront",
   "unit": 1, "terms": "/channel/terms"}}]}
```

`PaymentPayload.payload` by method:

* `tx`: `{"transaction": <signed pay tx, section 3>}`: the server checks it pays
  `payTo` exactly `amount` with an open challenge's memo, then puts it on chain
  (settles) and serves. Or `{"txid": "<64 hex>"}` for a tx the client already
  mined (used for txs too big for a header, e.g. an old Lamport key).
* `channel`: `{"channel": "<channel id>", "tick": i, "word": "<w_i, 64 hex>"}`, checked as in 1b.

`accepted` must echo this route's `scheme`, `network`, `asset`, `payTo` and `amount`.
Answers: `200` + `PAYMENT-RESPONSE {"success": true, "transaction": <txid, or "" for a
channel tick>, "network", "payer", "amount"}`; `400` for a malformed header
(`errorReason` `invalid_payload`, `invalid_x402_version`, `unsupported_scheme`,
`invalid_network`, `invalid_payment_requirements`); `402` for a refused payment, with
`PAYMENT-RESPONSE {"success": false, "errorReason", "errorMessage"}` and a fresh
`PAYMENT-REQUIRED`. paymesh-specific reasons: `invalid_paymesh_payload_recipient_mismatch`,
`invalid_paymesh_payload_value_mismatch`, `invalid_paymesh_challenge`,
`invalid_paymesh_channel_payment`, `invalid_paymesh_payment`; `invalid_transaction_state`
when the ledger refuses the tx.

A client that sent a signed tx and was refused retires that one-time key (a 1 PMX
payment to itself, sent to the node) before signing anything else, so a seller that
keeps a refused tx never holds two signatures from one key.

Tested against the reference Python SDK (`x402` 2.27.0 from PyPI, installed in a scratch
venv, never imported by paymesh): its parser reads our `PAYMENT-REQUIRED`, its `requests` wrapper pays
a route end to end through a small `paymesh` scheme plugin, and its own
`PaymentPayload` encoder pays a channel tick (`interop/x402_reference.py`). Not done: CORS headers for browser
clients, the facilitator endpoints (`/verify`, `/settle`, `/supported`), x402 v1.

The headers below (`X-Payment`, `X-Channel`) still work.

## 1. Paying for a call (paymesh-native headers)

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
6. Near expiry: a seller refuses ticks once the channel is within 20 blocks of
   `expires` (`402`, "too close to settle"). Blocks are cheap, so a buyer could
   still mine past the settler; therefore, when a `channel_refund` reaches the
   node, the node first settles the channel if it is the seller and was paid
   on it, and otherwise holds the refund up to 6 s (`PAYMESH_REFUND_HOLD_S`) so
   a seller running apart can settle first. Whichever is mined first counts.

## 2. Other endpoints

| method and path | answer |
|---|---|
| `GET /health` | `{"status": "ok", "chain_blocks": n}` |
| `GET /open/info` | free route: the price table |
| `GET /account?address=<addr>` | `{"balance", "current_key", "height", "channels"}`: what a client needs to sign its next tx |
| `GET /faucet?address=<addr>` | mints 100 PMX to `addr` (demo only); `?name=` creates a wallet on the server's machine |
| `POST /tx` | submit a signed tx (body up to 200 KB); `400 {"error"}` if any rule fails. Faucet txs are refused here. |
| `GET /tx?id=<txid>` | a mined tx (without its signature) and its block: for sellers running apart from the node |
| `GET /channel/info?id=<channel>` | a channel's terms and state: for sellers running apart from the node |
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

Let `m = sha256(from|to|amount|memo|txid|next_key[|json(kind, anchor, ticks, unit, expires, channel, word)])`.
The verifier tells the scheme apart by the signature's shape:

* **WOTS v2 (w=16)**, used for all new keys; `sig` has 68 elements. Key `i` has a
  public seed `P` (`sig[0]`). Every chain step is its own hash,
  `F(P, j, s, x) = sha256(P | 0x02 | byte j | byte s | x)`: the "simple"
  tweakable hash of SPHINCS+, in place of WOTS+ bitmasks. A preimage found for
  one chain position of one key is no use at any other position or key (no
  multi-target attacks), and v1/v2 values can't be swapped. The digest is
  `d = sha256("paymesh-wots2-msg" | P | m)`; its 64 base-16 digits plus a
  3-digit checksum (`sum(15 - d)`) say how far each of 67 chains is walked:
  `sig[1+j] = F` applied for steps `0 .. d_j - 1` to `sk_j`. The verifier walks
  steps `d_j .. 14` and compares `sha256("wots2" | P | pk_0 | ... | pk_66)` to
  the expected key fingerprint. About 4.98 KB per tx. On this machine, signing
  takes 1.6 ms (v1: 1.2 ms) and verifying 0.55 ms (v1: 0.43 ms).
* **WOTS v1** (keys made before wallet format 5): 67 elements, plain
  `sha256` chains, digest `m`, fingerprint `sha256("wots1" | pk_0 | ... | pk_66)`.
* **Lamport** (keys made before WOTS): 256 pairs of secrets; `pub` (512
  hashes) travels with the tx; fingerprint `sha256(concat(pub hex))`. About 52 KB.

Migration: an older wallet's current key is already named on chain, so it signs
once more in its own scheme and commits to a v2 key as `next_key`. From then on
the wallet signs only with v2 (wallet file format 5 records `wots2_from`). The
ledger keeps verifying all three schemes, so every block written before keeps
replaying.

## 5. Chain file

`chain.json` is a `{"format": 3}` line followed by one block per line
(append-only). A block is `{"index", "timestamp", "txs", "prev_hash", "nonce",
"hash"}`; its hash is `sha256("index|timestamp|json(txs, sorted keys)|prev_hash|nonce")`
and starts with `00` (proof of work, difficulty 2). The server is the only
miner; a miner thread puts every pending tx into its next block, and `POST /tx`
answers once the tx is mined. Every 500 blocks the ledger is checkpointed to
`chain.json.ckpt` (tip hash + ledger) to make restarts fast.

### Durability (`durable.py`)

| file | written | fsync (`PAYMESH_FSYNC=safe`, the default) |
|---|---|---|
| `chain.json` | one appended line per block | before `mine()` returns, so before any answer |
| `chain.json.ckpt`, a full chain rewrite | temp file + rename | before the rename |
| seller channel log | one appended line per tick, before the call is served | every 50 ms |
| new wallet seed | temp file + rename | before the rename |
| buyer channel seed | temp file + rename, before the deposit tx is sent | before the rename |

`strict` fsyncs every write, ticks included; `off` none. Every write
reaches the OS before the call that depends on it returns, so a killed process
loses nothing; fsync only matters if the OS itself loses its cache. On open,
a last line that a crash cut off is cut away (a complete record that only lost
its newline gets it back), so the next append starts on a clean line.
Measured on this machine (`bench.py 50 1000`, the same code back to back, calls/s):

| policy | on-chain | channel | 8 agents |
|---|---|---|---|
| off | 208 | 1,827 | 2,689 |
| safe | 196 | 1,708 | 2,544 |
| strict | 185 | 1,074 | 1,611 |
