# paymesh security: what is guaranteed, and how it is tested

paymesh is a proof of concept on play money; nothing here has been audited. Each guarantee
below is backed by a test in `tests/` or an attack in `attacks.py` (run both with
`py -3 check.py`). The limits are in the README.

## Guarantees, each covered by a test

`py -3 -m unittest discover -s tests` (137 tests: attacks, channels, HTTP end
to end, client SDK, a separate seller API, x402 headers, crash recovery, seeded fuzzing, wallets at rest).
`py -3 attacks.py` runs 52 attacks against a scratch node and shows each refused.
`py -3 check.py` runs everything (self-tests, suite, attacks, a quick benchmark) in about two
minutes, most of it the crash, fuzz and launcher tests.

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
  and its payouts and its close pay each seller exactly what it was credited
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
* **Browsers can pay, and only pay.** Paid routes answer any origin with the x402
  headers exposed; every other route refuses a request a browser marks as cross-site,
  so a page can't mint, post txs, open channels through `/tx` or read accounts
  (`tests/test_cors.py`, 4 attacks). `js/paymesh.js` signs byte for byte as Python
  does (`tests/test_js_client.py` compares them under Node.js).
* **A facilitator settles a payment once.** `/settle` answers `success` for a
  payment once; the settled mark is on disk before the answer, so a restart
  doesn't forget it.
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

