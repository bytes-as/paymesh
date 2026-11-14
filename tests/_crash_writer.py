"""Helper for test_durability: mine blocks and log channel ticks as fast as possible,
printing what has been acknowledged, until the test kills this process.

    py -3 _crash_writer.py <dir>
prints "block <height>" after mine() returns and "tick <i>" after SellerBook._record returns.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import chain as ch  # noqa: E402
import channels  # noqa: E402

d = sys.argv[1]
ch.CKPT_EVERY = 7  # checkpoints and compactions often, so kills land in them too
channels.SellerBook.COMPACT_AT = 40
c = ch.Chain(os.path.join(d, "chain.json"))
if not c.blocks:
    c.genesis()
book = channels.SellerBook(os.path.join(d, "seller.log"), "aa" * 32)
cid = "cc" * 32
i = (book.best(cid) or {"i": 0})["i"]
out = sys.stdout
while True:
    for k in range(12):  # ~3 KB blocks: big writes
        c.add_tx(c.new_tx(ch.FAUCET, f"{k + 1:02x}" * 32, 1, "faucet:" + "m" * 200))
    b = c.mine()
    out.write(f"block {len(c.blocks)}\n")
    out.flush()
    for _ in range(20):
        i += 1
        book.book[cid] = {"i": i, "w": f"{i:064x}"}
        book._record(cid, i, f"{i:064x}")
        out.write(f"tick {i}\n")
        out.flush()
