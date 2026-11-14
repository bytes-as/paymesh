"""paymesh wallet: a command line for people (agents use paymesh_client.py).

    py -3 wallet.py new <name> [--encrypt]     create a wallet (keys stay in wallets/);
                                               --encrypt seals its seed with PAYMESH_PASSPHRASE
    py -3 wallet.py address <name>
    py -3 wallet.py balance <name>
    py -3 wallet.py faucet <name>              demo money: +100 PMX
    py -3 wallet.py send <name> <to> <amount> [memo]
                                               <to> is an address or a local wallet name
    py -3 wallet.py history <name> [n]         last n txs to or from this wallet
    py -3 wallet.py channels <name>            open payment channels and their spend

Talks to the API at PAYMESH_API (default http://127.0.0.1:8402). Stdlib only.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import chain as ch  # noqa: E402
import wallets  # noqa: E402
from paymesh_client import Client, PaymentError  # noqa: E402

API = os.environ.get("PAYMESH_API", f"http://127.0.0.1:{os.environ.get('PAYMESH_API_PORT', 8402)}")


def _client(name):
    if not os.path.exists(os.path.join(wallets.WALLET_DIR, f"{wallets.check_name(name)}.wallet.json")):
        raise SystemExit(f"no wallet '{name}': create it with  py -3 wallet.py new {name}")
    return Client(API, name)


def _resolve(to: str) -> str:
    if ch.HEX64.match(to):
        return to
    try:
        return wallets.load(to).address
    except (FileNotFoundError, ValueError, LookupError):
        raise SystemExit(f"'{to}' is neither a 64-hex address nor a local wallet name") from None


def main(argv):
    if len(argv) < 2 or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, name = argv[0], argv[1]
    if cmd == "new":
        pw = None
        if "--encrypt" in argv[2:]:
            pw = os.environ.get("PAYMESH_PASSPHRASE")
            if not pw:
                raise SystemExit("--encrypt needs the passphrase in PAYMESH_PASSPHRASE")
        w = wallets.create(name, pw)
        print(json.dumps({"wallet": name, "address": w.address, "encrypted": bool(pw)}))
        return 0
    c = _client(name)
    if cmd == "address":
        print(c.wallet.address)
    elif cmd == "balance":
        print(json.dumps({"wallet": name, "balance": c.balance(), "currency": "PMX"}))
    elif cmd == "faucet":
        r = c.faucet()
        print(json.dumps({"wallet": name, "minted": r["minted"], "balance": c.balance()}))
    elif cmd == "send":
        if len(argv) < 4:
            raise SystemExit("usage: wallet.py send <name> <to> <amount> [memo]")
        to, amount = _resolve(argv[2]), int(argv[3])
        memo = argv[4] if len(argv) > 4 else "transfer"
        led = c._ledger()
        tx = led.new_tx(c.wallet.address, to, amount, memo)
        wallets.sign_tx(tx, c.wallet, led)
        r = c._submit(tx)
        print(json.dumps({"sent": amount, "to": to, "txid": r["txid"], "block": r["block"], "balance": c.balance()}))
    elif cmd == "history":
        n = int(argv[2]) if len(argv) > 2 else 20
        chain = ch.Chain()  # the local chain file, read-only
        me = c.wallet.address
        rows = [tx for tx in chain.all_txs() if me in (tx["from_wallet"], tx["to_wallet"])][-n:]
        for tx in rows:
            out = tx["from_wallet"] == me and tx.get("kind", "pay") != "channel_close"
            print(json.dumps({"txid": tx["txid"][:16], "kind": tx.get("kind", "pay"), "dir": "out" if out else "in",
                              "amount": tx["amount"], "other": (tx["to_wallet"] if out else tx["from_wallet"])[:16],
                              "memo": tx["memo"][:40]}))
    elif cmd == "channels":
        acc = c._ledger()
        for cid, chn in acc.channels.items():
            code, st = c._req(f"/channel/state?channel={cid}")
            print(json.dumps({"channel": cid[:16], "deposit": chn["n"] * chn["unit"],
                              "spent": st.get("paid_ticks", 0) * chn["unit"] if code == 200 else "?",
                              "expires_at_block": chn["expires"]}))
        if not acc.channels:
            print(json.dumps({"wallet": name, "open_channels": 0}))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (PaymentError, OSError, ValueError) as e:  # PermissionError is an OSError
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
