# paymesh benchmarks

`py -3 bench.py 100 2000`, on a separate server (never the live one), this machine
(20 logical CPUs, Windows, Python 3.14) while otherwise idle, 2026-11-14:

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

Numbers move with the machine's load. On 2026-11-15, with the CPU about 30 % busy with
other programs, the same command gave a median of 172 / 1,713 / 1,802 calls per second
over three runs, and the last pushed version (`cf0848d`), run alternately with it, gave
162 / 1,575 / 1,628. The features added since then cost nothing measurable.
In the same conditions a hub channel (one deposit that pays any seller, every word
through the hub's book, payouts running) did a median 1,288 paid calls per second
(p50 0.66 ms) against 1,703 (0.55 ms) for a direct channel; `bench.py` measures both.


On GitHub's CI machines (`check.py`'s short run, `bench.py 50 1000`, 2026-11-15), Ubuntu did
189–196 on-chain, 2,186–2,458 channel and 2,209–2,313 hub calls per second; Windows 67–82,
1,282–1,422 and 1,237–1,401. The first CI run had shown 24 channel calls per second on Ubuntu:
a reply's headers and body left in separate writes and waited ~40 ms for a delayed ACK. The
HTTP handlers now set TCP_NODELAY.
