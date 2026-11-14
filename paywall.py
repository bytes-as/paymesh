"""paymesh 402 paywall middleware: drop onto any BaseHTTPRequestHandler.

Flow:
 1. Unpaid GET -> HTTP 402 with a challenge JSON:
        {"recipient", "amount", "nonce", "memo", "how_to_pay"}
 2. Client sends a chain tx whose memo is exactly the challenge memo, then
    retries with header "X-Payment: <txid>".
 3. Server finds the tx in a mined block and checks that its memo is this
    challenge's memo, it pays the quoted recipient and amount, the path
    matches, and the challenge is open. Only then is the challenge spent, so
    a wrong or hostile payment cannot burn someone else's challenge; a spent
    challenge (or txid) never verifies again.

Open challenges expire after CHALLENGE_TTL seconds and at most MAX_OPEN are
kept (oldest dropped first), so a flood of unpaid requests can't grow memory
without bound. Stdlib only.
"""
import collections
import os
import secrets
import threading
import time

CHALLENGE_TTL = 300.0
MAX_OPEN = 10_000
CURRENCY = "PMX"

# nonce -> {"recipient", "amount", "path", "expires"}, oldest first
_open_challenges: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
_spent_txids: set = set()
_lock = threading.Lock()


def _sweep(now: float):
    while _open_challenges:
        nonce, ch = next(iter(_open_challenges.items()))
        if ch["expires"] > now and len(_open_challenges) <= MAX_OPEN:
            break
        _open_challenges.pop(nonce)


def open_challenge(chain, recipient: str, amount: int, path: str) -> dict:
    """Mint a payment challenge; the tx must carry the returned memo."""
    nonce = secrets.token_hex(16)
    memo = f"{recipient}:{nonce}:{amount}"
    now = time.time()
    with _lock:
        _open_challenges[nonce] = {"recipient": recipient, "amount": amount, "path": path,
                                   "memo": memo, "expires": now + CHALLENGE_TTL}
        _sweep(now)
    return {
        "error": "payment required",
        "recipient": recipient,
        "amount": amount,
        "currency": CURRENCY,
        "memo": memo,
        "nonce": nonce,
        "expires_in": int(CHALLENGE_TTL),
        "chain_tip": chain.tip_hash,
        "how_to_pay": {
            "step1": f"send a signed chain tx: to={recipient}, amount={amount}, memo=memo (POST /tx)",
            "step2": "the server mines it at once",
            "step3": "retry this request with header X-Payment: <txid>",
        },
    }


def challenge_open(memo: str, path: str, recipient: str, amount: int) -> tuple[bool, str]:
    """Read-only: would a tx with this memo, recipient and amount pay an open challenge for path?
    Lets a server refuse a signed tx before it moves the buyer's money."""
    parts = memo.split(":") if isinstance(memo, str) else []
    if len(parts) != 3:
        return False, "tx memo is not a payment challenge"
    with _lock:
        ch = _open_challenges.get(parts[1])
        if ch is None or time.time() > ch["expires"]:
            return False, "unknown, expired or already-spent payment nonce"
        if ch["path"] != path:
            return False, "challenge was for a different path"
        if memo != ch["memo"] or recipient != ch["recipient"] or amount != ch["amount"]:
            return False, "tx does not match the challenge memo/recipient/amount"
    return True, "open"


def reserve(memo: str, path: str, recipient: str, amount: int, txid: str) -> tuple[bool, str]:
    """Like challenge_open, and atomically claim the challenge for this txid: a second tx
    for the same challenge (a retry, or two requests at once) is refused before it can
    move the buyer's money. release() undoes it if the tx is then refused."""
    ok, why = challenge_open(memo, path, recipient, amount)
    if not ok:
        return ok, why
    with _lock:
        ch = _open_challenges.get(memo.split(":")[1])
        if ch is None:
            return False, "unknown, expired or already-spent payment nonce"
        if ch.get("reserved_by") not in (None, txid):
            return False, "another tx is already paying this challenge"
        ch["reserved_by"] = txid
    return True, "reserved"


def release(memo: str, txid: str):
    with _lock:
        ch = _open_challenges.get(memo.split(":")[1])
        if ch is not None and ch.get("reserved_by") == txid:
            ch.pop("reserved_by")


def verify_payment(chain, headers, path: str) -> tuple[bool, str]:
    """Return (ok, reason). ok=True only when a mined tx pays this exact open challenge."""
    txid = headers.get("X-Payment", "") or ""
    if not txid:
        return False, "no X-Payment header"
    return verify_txid(chain, txid.strip(), path)


def verify_txid(chain, txid: str, path: str) -> tuple[bool, str]:
    tx, block = chain.find_tx_by_id(txid)
    if tx is None:
        return False, "txid not found in any mined block"
    parts = tx.get("memo", "").split(":")
    if len(parts) != 3:
        return False, "tx memo is not a payment challenge"
    nonce = parts[1]
    with _lock:
        if tx["txid"] in _spent_txids:
            return False, "this payment was already used"
        ch = _open_challenges.get(nonce)
        if ch is None:
            return False, "unknown, expired or already-spent payment nonce"
        if time.time() > ch["expires"]:
            _open_challenges.pop(nonce, None)
            return False, "challenge expired"
        if ch["path"] != path:
            return False, "challenge was for a different path"
        if tx["memo"] != ch["memo"]:
            return False, "tx memo does not match the challenge"
        if tx["to_wallet"] != ch["recipient"] or tx["amount"] != ch["amount"]:
            return False, "tx does not pay the exact challenge recipient/amount"
        _open_challenges.pop(nonce)  # spend only after every check passed
        _spent_txids.add(tx["txid"])
    return True, f"paid by tx {tx['txid'][:16]} in block {block.index}"


if __name__ == "__main__":
    import tempfile
    import chain as ch
    import wallets

    tmp = tempfile.mkdtemp()
    wallets.WALLET_DIR = os.path.join(tmp, "wallets")
    c = ch.Chain(os.path.join(tmp, "chain.json"))
    c.genesis()
    seller = wallets.fund(c, "seller", 0)
    buyer = wallets.fund(c, "buyer", 1000)
    c.mine()

    class FakeHeaders(dict):
        def get(self, k, default=None):
            return dict.get(self, k, default)

    challenge = open_challenge(c, seller.address, 5, "/premium/price")
    tx = c.new_tx(buyer.address, seller.address, 5, challenge["memo"])
    wallets.sign_tx(tx, buyer, c)
    c.add_tx(tx)
    c.mine()
    ok, why = verify_payment(c, FakeHeaders({"X-Payment": tx["txid"]}), "/premium/price")
    assert ok, why
    ok2, why2 = verify_payment(c, FakeHeaders({"X-Payment": tx["txid"]}), "/premium/price")
    assert not ok2, why2
    print("self-test ok: 402 -> pay -> verified; replay rejected")
