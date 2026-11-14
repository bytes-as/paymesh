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


# --- browsers (CORS) ---------------------------------------------------------------------------
# Paid routes are open to any origin: an x402 call carries its own payment and no cookie or other
# ambient credential, so a page on another site can only spend what its own client signs. Every
# other route refuses a request that a browser marks as coming from another site: CORS alone only
# hides responses, while a simple GET (<img>) or a text/plain POST would still be sent and act.
# Non-browser clients send neither header and are not affected.
def cors_headers() -> list:
    """On every response of a paid route: readable from any origin, x402 headers exposed."""
    return [("Access-Control-Allow-Origin", "*"), ("Access-Control-Expose-Headers", f"{REQUIRED}, {RESPONSE}")]


def preflight_headers() -> list:
    """The answer to OPTIONS on a paid route: a GET with PAYMENT-SIGNATURE may be sent."""
    return cors_headers() + [("Access-Control-Allow-Methods", "GET"), ("Access-Control-Allow-Headers", SIGNATURE),
                             ("Access-Control-Max-Age", "600")]


def cross_site(header, host: str):
    """Why a request to a non-x402 route came from another site, or None. header(name) -> value."""
    site = (header("Sec-Fetch-Site") or "").lower()
    if site in ("cross-site", "same-site"):
        return f"Sec-Fetch-Site: {site}"
    origin = header("Origin")
    if origin is not None and origin not in (f"http://{host}", f"https://{host}"):
        return f"Origin {origin[:80]} is not this server"
    return None
MAX_HEADER = 48_000  # base64 chars: a WOTS-signed tx is ~6.6 K; a Lamport one goes by txid instead
METHODS = ("tx", "channel")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class Pending(ValueError):
    """A submitted tx was accepted but not mined in time: it may still be mined. Never a refusal."""


PENDING_MSG = "not mined in time; it stays pending"


class Invalid(ValueError):
    """A payment header that can't be used. `status` 400 = malformed, 402 = refused."""

    def __init__(self, reason: str, message: str, status: int = 400):
        super().__init__(message)
        self.reason, self.status = reason, status


def network_id(genesis_hash: str) -> str:
    return f"{SCHEME}:{genesis_hash[:32]}"


def loads(raw):
    """json.loads for untrusted input: nesting deep enough to overflow the parser's stack
    (about 5,000 to 17,000 brackets, depending on the platform) raises RecursionError, which
    is not a ValueError; here it is one, so callers answer 400 instead of dropping the request."""
    try:
        return json.loads(raw)
    except RecursionError:
        raise ValueError("JSON nested too deeply") from None


def encode(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode("ascii")


def decode(value: str) -> dict:
    if not isinstance(value, str) or not value.strip():
        raise Invalid("invalid_payload", "empty header")
    value = value.strip()
    if len(value) > MAX_HEADER:
        raise Invalid("invalid_payload", f"header over {MAX_HEADER} chars")
    try:
        obj = loads(base64.b64decode(value, validate=True).decode("utf-8"))
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
                    hub: str = "", height: int | None = None) -> list:
    """The two ways to pay a route: a tx carrying this challenge's memo, or a channel tick
    (from a channel to this seller, or to `hub`, whose channels any seller takes). `height`
    (the chain's) lets a client that can't ask the node (a browser page) set a new channel's
    expiry when it opens one inside its payment ({"open": channel_open tx, "tick", "word"})."""
    t = challenge["expires_in"]
    on_chain = requirement(network, pay_to, price, "tx", t, memo=challenge["memo"], nonce=challenge["nonce"],
                           **({"node": node} if node else {}))
    return [on_chain, requirement(network, pay_to, price, "channel", t, unit=unit, terms="/channel/terms",
                                  **({"hub": hub} if hub else {}), **({"height": height} if height is not None else {}))]


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


def parse(value: str, network: str, pay_to: str, price: int, hub: str | None = None) -> tuple[str, dict]:
    """Check a PAYMENT-SIGNATURE against what this route quotes. Returns (method, payload);
    raises Invalid. Nothing here touches the chain or spends anything. `hub`: a channel opened
    inside the payment may lock its deposit for this hub (whose channels pay any seller)."""
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
        op = payload.get("open")
        if op is not None:  # open a channel to this seller and pay its first ticks, in one request
            if not isinstance(op, dict) or op.get("kind") != "channel_open" or not isinstance(op.get("txid"), str) \
                    or not _HEX64.match(op["txid"]) or set(payload) != {"open", "tick", "word"}:
                raise Invalid("invalid_payload", "an opening payload is {open: <signed channel_open tx>, tick, word}")
            if op.get("to_wallet") not in (pay_to, hub or pay_to):
                raise Invalid("invalid_paymesh_payload_recipient_mismatch", "the channel_open pays someone else", 402)
            payload = dict(payload, channel=op["txid"])
        cid, tick, word = payload.get("channel"), payload.get("tick"), payload.get("word")
        if not (isinstance(cid, str) and _HEX64.match(cid) and type(tick) is int and tick > 0
                and isinstance(word, str) and _HEX64.match(word)) or len(payload) != (4 if op is not None else 3):
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


def settle(value: str, *, network: str, pay_to: str, price: int, path: str, chain_, book, submit, lock=None,
           hub: str | None = None):
    """Verify and settle one PAYMENT-SIGNATURE for `path`. Returns (status, SettlementResponse, method):
    200 paid, 400 malformed, 402 refused. A challenge or channel word is spent only on 200.

    chain_: find_tx_by_id and ledger.channels (a Chain, or a node seen over HTTP);
    book: channels.SellerBook; submit(tx): put a signed tx on chain (raise ValueError if refused)."""
    try:
        method, payload = parse(value, network, pay_to, price, hub)
    except Invalid as e:
        return e.status, settlement(network, False, reason=e.reason, message=str(e)), None
    if method == "channel":
        if "open" in payload:  # mine the channel_open first (only on a 200 for it is the word taken)
            op = payload["open"]
            try:
                submit(op)
            except Pending as e:
                return 402, settlement(network, False, op["txid"], reason="settlement_pending", message=str(e)), method
            except ValueError as e:
                if "txid already used" not in str(e):
                    return 402, settlement(network, False, reason="invalid_transaction_state", message=str(e)), method
                if chain_.find_tx_by_id(op["txid"])[0] is None:
                    return 402, settlement(network, False, op["txid"], reason="settlement_pending",
                                           message=PENDING_MSG), method
        ok, why = book.accept(chain_, f"{payload['channel']} {payload['tick']} {payload['word']}", price)
        if not ok:
            return 402, settlement(network, False, reason="invalid_paymesh_channel_payment", message=why), method
        c = chain_.ledger.channels.get(payload["channel"]) or {}
        return 200, settlement(network, True, "", c.get("buyer", ""), price), method
    if "transaction" in payload:
        tx = payload["transaction"]
        # refuse before the money moves: an unusable challenge must not cost the buyer, and
        # only one tx per challenge (reserved atomically) may go on chain
        ok, why = paywall.reserve(tx["memo"], path, pay_to, price, tx["txid"])
        if not ok:
            return 402, settlement(network, False, reason="invalid_paymesh_challenge", message=why), method
        try:
            submit(tx)
        except Pending as e:  # it may still be mined: keep the reservation, so no other tx can pay this challenge
            return 402, settlement(network, False, tx["txid"], reason="settlement_pending", message=str(e)), method
        except ValueError as e:
            if "txid already used" in str(e):  # a retry of a tx the ledger already has
                if chain_.find_tx_by_id(tx["txid"])[0] is None:  # still pending
                    return 402, settlement(network, False, tx["txid"], reason="settlement_pending",
                                           message=PENDING_MSG), method
            else:  # a definite refusal: the tx can never be mined, the challenge is free again
                paywall.release(tx["memo"], tx["txid"])
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
