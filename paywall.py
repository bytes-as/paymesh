"""paymesh 402 paywall middleware: drop onto any BaseHTTPRequestHandler.

Flow:
 1. Unauthenticated GET -> HTTP 402 with a challenge JSON:
        {"recipient", "amount", "nonce", "memo", "how_to_pay"}
 2. Client sends a chain tx with memo == challenge memo, then retries with
    header "X-Payment": <txid>.
 3. Server looks the tx up by txid, checks it is in a mined block, memo binds
    recipient + amount + path exactly, and the nonce is unspent. Replaying the
    same header, or re-paying, fails because each nonce is consumed once.

Challenge nonces expire after CHALLENGE_TTL seconds. Stdlib only.
"""
import json
import os
import secrets
import time

CHALLENGE_TTL = 300.0
CURRENCY = "PMX"

# nonce -> {"recipient", "amount", "path", "expires"}
_open_challenges: dict = {}


def open_challenge(chain, recipient: str, amount: int, path: str) -> dict:
    """Mint a payment challenge; the tx must carry the returned memo."""
    nonce = secrets.token_hex(16)
    _open_challenges[nonce] = {
        "recipient": recipient,
        "amount": amount,
        "path": path,
        "expires": time.time() + CHALLENGE_TTL,
    }
    return {
        "error": "payment required",
        "recipient": recipient,
        "amount": amount,
        "currency": CURRENCY,
        "memo": f"{recipient}:{nonce}:{amount}",
        "nonce": nonce,
        "chain_tip": chain.tip_hash,
        "how_to_pay": {
            "step1": f"send a chain tx: to={recipient}, amount={amount}, memo=memo",
            "step2": "wait for it to be mined (PoW difficulty 2, <1s)",
            "step3": "retry this request with header X-Payment: <txid>",
        },
    }


def verify_payment(chain, headers, path: str) -> tuple[bool, str]:
    """Return (ok, reason). ok=True only when the tx pays this exact nonce once."""
    txid = headers.get("X-Payment", "")
    if not txid:
        return False, "no X-Payment header"
    tx, block = chain.find_tx_by_id(txid)
    if tx is None:
        return False, "txid not found in any mined block"
    nonce = tx["memo"].split(":")[1]
    ch = _open_challenges.pop(nonce, None)
    if ch is None:
        return False, "unknown or already-spent payment nonce"
    if time.time() > ch["expires"]:
        return False, "challenge expired"
    if ch["path"] != path:
        return False, "challenge was for a different path"
    if tx["to_wallet"] != ch["recipient"] or tx["amount"] != ch["amount"]:
        return False, "tx does not pay the exact challenge recipient/amount"
    return True, f"paid by tx {tx['txid'][:16]} in block {block.index}"


if __name__ == "__main__":
    import chain as ch
    import wallets

    selftest = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest-chain.json")
    if os.path.exists(selftest):
        os.remove(selftest)
    c = ch.Chain(selftest)
    c.genesis()
    seller = wallets.fund(c, "seller", 0)
    buyer = wallets.fund(c, "buyer", 1000)
    c.mine()

    class FakeHeaders(dict):
        def get(self, k, default=None):
            return dict.get(self, k, default)

    # 1. challenge
    challenge = open_challenge(c, seller.address, 5, "/premium/price")
    # 2. pay (signed with the buyer's Lamport key)
    tx = c.new_tx(buyer.address, seller.address, 5, challenge["memo"])
    wallets.sign_tx(tx, buyer)
    c.add_tx(tx)
    c.mine()
    # 3. verify ok
    ok, why = verify_payment(c, FakeHeaders({"X-Payment": tx["txid"]}), "/premium/price")
    assert ok, why
    # 4. replay: same header again -> nonce already spent
    ok2, why2 = verify_payment(c, FakeHeaders({"X-Payment": tx["txid"]}), "/premium/price")
    assert not ok2, why2
    # 5. wrong recipient -> rejected before nonce consumed? (nonce consumed here, fine)
    challenge2 = open_challenge(c, seller.address, 5, "/premium/price")
    bad = c.new_tx(buyer.address, buyer.address, 5, challenge2["memo"])
    wallets.sign_tx(bad, buyer)  # correctly signed, but pays the wrong recipient
    c.add_tx(bad)
    c.mine()
    ok3, why3 = verify_payment(c, FakeHeaders({"X-Payment": bad["txid"]}), "/premium/price")
    assert not ok3, why3
    print("self-test ok: 402 -> pay -> verified; replay and wrong-recipient rejected")
