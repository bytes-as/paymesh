"""x402 v2 wire format for paymesh: the "paymesh" scheme in the spec's HTTP headers.

Spec: x402 Protocol Version 2, specs/x402-specification-v2.md and
specs/transports-v2/http.md in github.com/x402-foundation/x402 at commit
31ce6bb (2026-11-12). Three headers, each base64 of a JSON object:

    PAYMENT-REQUIRED   server -> client, on the 402   PaymentRequired
    PAYMENT-SIGNATURE  client -> server, on the retry PaymentPayload
    PAYMENT-RESPONSE   server -> client               SettlementResponse

Scheme "paymesh", network "paymesh:<first 32 hex of the genesis hash>"
(CAIP-2 shape), asset "PMX", amounts as decimal strings of whole PMX.
Two asset transfer methods, both settled before the resource runs
(paymentFlow "upfront"):

    "tx"       payload {"transaction": <signed pay tx>}  the server submits it
               or      {"txid": <64 hex>}                 already mined by the client
    "channel"  payload {"channel": <id>, "tick": i, "word": <64 hex>}

Stdlib only.
"""
import base64
import binascii
import json
import re

import paywall

VERSION = 2
SCHEME = "paymesh"
ASSET = "PMX"
FLOW = "upfront"
SPEC = "x402 v2, x402-foundation/x402@31ce6bb (2026-11-12)"
REQUIRED, SIGNATURE, RESPONSE = "PAYMENT-REQUIRED", "PAYMENT-SIGNATURE", "PAYMENT-RESPONSE"
MAX_HEADER = 48_000  # base64 chars: a WOTS-signed tx is ~6.6 K; a Lamport one goes by txid instead
METHODS = ("tx", "channel")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class Invalid(ValueError):
    """A payment header that can't be used. `status` 400 = malformed, 402 = refused."""

    def __init__(self, reason: str, message: str, status: int = 400):
        super().__init__(message)
        self.reason, self.status = reason, status


def network_id(genesis_hash: str) -> str:
    return f"{SCHEME}:{genesis_hash[:32]}"


def encode(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode("ascii")


def decode(value: str) -> dict:
    if not isinstance(value, str) or not value.strip():
        raise Invalid("invalid_payload", "empty header")
    value = value.strip()
    if len(value) > MAX_HEADER:
        raise Invalid("invalid_payload", f"header over {MAX_HEADER} chars")
    try:
        obj = json.loads(base64.b64decode(value, validate=True).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise Invalid("invalid_payload", "header is not base64 of a JSON object") from None
    if not isinstance(obj, dict):
        raise Invalid("invalid_payload", "header is not base64 of a JSON object")
    return obj


def requirement(network: str, pay_to: str, amount: int, method: str, timeout: int, **extra) -> dict:
    """One PaymentRequirements entry for `accepts`."""
    return {"scheme": SCHEME, "network": network, "amount": str(amount), "asset": ASSET, "payTo": pay_to,
            "maxTimeoutSeconds": int(timeout),
            "extra": dict(extra, assetTransferMethod=method, paymentFlow=FLOW)}


def paymesh_accepts(network: str, pay_to: str, price: int, challenge: dict, unit: int, node: str = "",
                    hub: str = "") -> list:
    """The two ways to pay a route: a tx carrying this challenge's memo, or a channel tick
    (from a channel to this seller, or to `hub`, whose channels any seller takes)."""
    t = challenge["expires_in"]
    on_chain = requirement(network, pay_to, price, "tx", t, memo=challenge["memo"], nonce=challenge["nonce"],
                           **({"node": node} if node else {}))
    return [on_chain, requirement(network, pay_to, price, "channel", t, unit=unit, terms="/channel/terms",
                                  **({"hub": hub} if hub else {}))]


def payment_required(url: str, accepts: list, error: str = "PAYMENT-SIGNATURE header is required",
                     description: str = "") -> dict:
    resource = {"url": url, "mimeType": "application/json"}
    if description:
        resource["description"] = description
    return {"x402Version": VERSION, "error": error, "resource": resource, "accepts": accepts}


def settlement(network: str, success: bool, transaction: str = "", payer: str = "", amount=None,
               reason: str = "", message: str = "") -> dict:
    r = {"success": success, "transaction": transaction, "network": network}
    if payer:
        r["payer"] = payer
    if amount is not None:
        r["amount"] = str(amount)
    if not success:
        r["errorReason"] = reason or "unexpected_settle_error"
        if message:
            r["errorMessage"] = message[:200]
    return r


def parse(value: str, network: str, pay_to: str, price: int) -> tuple[str, dict]:
    """Check a PAYMENT-SIGNATURE against what this route quotes. Returns (method, payload);
    raises Invalid. Nothing here touches the chain or spends anything."""
    p = decode(value)
    if p.get("x402Version") != VERSION:
        raise Invalid("invalid_x402_version", "x402Version must be 2")
    acc, payload = p.get("accepted"), p.get("payload")
    if not isinstance(acc, dict) or not isinstance(payload, dict):
        raise Invalid("invalid_payload", "accepted and payload must be objects")
    if acc.get("scheme") != SCHEME:
        raise Invalid("unsupported_scheme", f"scheme must be {SCHEME!r}")
    if acc.get("network") != network:
        raise Invalid("invalid_network", f"network must be {network}")
    if acc.get("asset") != ASSET or acc.get("payTo") != pay_to or acc.get("amount") != str(price):
        raise Invalid("invalid_payment_requirements", "accepted asset/payTo/amount differ from this route's quote")
    extra = acc.get("extra") if isinstance(acc.get("extra"), dict) else {}
    method = extra.get("assetTransferMethod", "tx")
    if method not in METHODS:
        raise Invalid("invalid_payment_requirements", f"assetTransferMethod must be one of {METHODS}")
    if extra.get("paymentFlow", FLOW) != FLOW:
        raise Invalid("invalid_payment_requirements", f"paymentFlow must be {FLOW!r}")
    if method == "channel":
        cid, tick, word = payload.get("channel"), payload.get("tick"), payload.get("word")
        if not (isinstance(cid, str) and _HEX64.match(cid) and type(tick) is int and tick > 0
                and isinstance(word, str) and _HEX64.match(word)) or len(payload) != 3:
            raise Invalid("invalid_payload", "channel payload is {channel: 64 hex, tick: int > 0, word: 64 hex}")
        return method, payload
    if set(payload) == {"txid"} and isinstance(payload["txid"], str) and _HEX64.match(payload["txid"]):
        return method, payload
    if set(payload) == {"transaction"} and isinstance(payload["transaction"], dict):
        tx = payload["transaction"]
        if tx.get("kind", "pay") != "pay":
            raise Invalid("invalid_payload", "the transaction must be a pay tx")
        if tx.get("to_wallet") != pay_to:
            raise Invalid("invalid_paymesh_payload_recipient_mismatch", "the tx pays someone else", 402)
        if tx.get("amount") != price or type(tx.get("amount")) is not int:
            raise Invalid("invalid_paymesh_payload_value_mismatch", f"the tx must pay exactly {price}", 402)
        if not isinstance(tx.get("memo"), str) or not isinstance(tx.get("txid"), str):
            raise Invalid("invalid_payload", "the tx needs a memo and a txid")
        return method, payload
    raise Invalid("invalid_payload", "tx payload is {transaction: <signed tx>} or {txid: 64 hex}")


def settle(value: str, *, network: str, pay_to: str, price: int, path: str, chain_, book, submit, lock=None):
    """Verify and settle one PAYMENT-SIGNATURE for `path`. Returns (status, SettlementResponse, method):
    200 paid, 400 malformed, 402 refused. A challenge or channel word is spent only on 200.

    chain_: find_tx_by_id and ledger.channels (a Chain, or a node seen over HTTP);
    book: channels.SellerBook; submit(tx): put a signed tx on chain (raise ValueError if refused)."""
    try:
        method, payload = parse(value, network, pay_to, price)
    except Invalid as e:
        return e.status, settlement(network, False, reason=e.reason, message=str(e)), None
    if method == "channel":
        ok, why = book.accept(chain_, f"{payload['channel']} {payload['tick']} {payload['word']}", price)
        if not ok:
            return 402, settlement(network, False, reason="invalid_paymesh_channel_payment", message=why), method
        c = chain_.ledger.channels.get(payload["channel"]) or {}
        return 200, settlement(network, True, "", c.get("buyer", ""), price), method
    if "transaction" in payload:
        tx = payload["transaction"]
        ok, why = paywall.challenge_open(tx["memo"], path, pay_to, price)
        if not ok:  # refuse before the money moves: an unusable challenge must not cost the buyer
            return 402, settlement(network, False, reason="invalid_paymesh_challenge", message=why), method
        try:
            submit(tx)
        except ValueError as e:
            return 402, settlement(network, False, reason="invalid_transaction_state", message=str(e)), method
        txid = tx["txid"]
    else:
        txid = payload["txid"]
    with lock if lock is not None else _Null():
        ok, why = paywall.verify_txid(chain_, txid, path)
    if not ok:
        return 402, settlement(network, False, txid if "transaction" in payload else "",
                               reason="invalid_paymesh_payment", message=why), method
    tx, _ = chain_.find_tx_by_id(txid)
    return 200, settlement(network, True, txid, (tx or {}).get("from_wallet", ""), price), method


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
