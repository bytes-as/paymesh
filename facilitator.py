"""x402 facilitator for the "paymesh" scheme: POST /verify, POST /settle, GET /supported.

Spec: x402 v2, specs/x402-specification-v2.md section 7, x402-foundation/x402@31ce6bb.
A resource server that does not run paymesh itself sends the buyer's PaymentPayload and its
own PaymentRequirements here; the node checks (verify, read-only) or settles them. Both
answer HTTP 200 with a VerifyResponse / SettlementResponse, valid or not (a malformed
request gets 400), as the reference facilitator client expects.

What the node can settle per transfer method:
  tx       {"transaction": signed pay tx}: verify checks it against the ledger without
           applying it; settle puts it on chain (a timeout is settlement_pending).
           {"txid": mined tx}: checked on chain.
  channel  words of a channel opened to the node's hub only: the hub credits payTo and pays
           it in its close (splits). A direct channel is settled by its own seller.
A payment settles once: settled txids are logged durably, so a resource server can't be
told "success" twice for one payment, also across node restarts. When the requirements
carry extra.memo, the tx memo must equal it (binds a payment to one resource's challenge).
Stdlib only.
"""
import os
import threading
import time

import durable
import paymesh_x402 as x402


class Facilitator:
    def __init__(self, chain_, chain_lock, submit, hub_book, hub_address: str, network: str, log_path: str):
        self.chain, self.lock, self.submit = chain_, chain_lock, submit
        self.hub, self.hub_address, self.network = hub_book, hub_address, network
        self.settled: set = set()
        self.busy: set = set()  # txids being settled right now
        self.mutex = threading.Lock()
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                self.settled = {ln.strip() for ln in f if len(ln.strip()) == 64}
        self.log = durable.AppendLog(log_path, critical=True)  # a "settled" mark must survive a crash

    def supported(self) -> dict:
        return {"kinds": [{"x402Version": x402.VERSION, "scheme": x402.SCHEME, "network": self.network,
                           "extra": {"assetTransferMethods": list(x402.METHODS), "channels": "hub",
                                     "hub": self.hub_address, "asset": x402.ASSET}}],
                "extensions": [], "signers": {"paymesh:*": [self.hub_address]}}

    # --- request checks shared by verify and settle ---
    def _parse(self, body):
        """(method, inner payload, requirements) or raise x402.Invalid."""
        if not isinstance(body, dict) or body.get("x402Version") != x402.VERSION:
            raise x402.Invalid("invalid_x402_version", "x402Version must be 2")
        payload, req = body.get("paymentPayload"), body.get("paymentRequirements")
        if not isinstance(payload, dict) or not isinstance(req, dict):
            raise x402.Invalid("invalid_payload", "paymentPayload and paymentRequirements must be objects")
        if req.get("scheme") != x402.SCHEME:
            raise x402.Invalid("unsupported_scheme", f"this facilitator settles scheme {x402.SCHEME!r}")
        if req.get("network") != self.network:
            raise x402.Invalid("invalid_network", f"network must be {self.network}")
        amount, pay_to = req.get("amount"), req.get("payTo")
        if not (isinstance(amount, str) and amount.isascii() and amount.isdigit() and int(amount) > 0
                and isinstance(pay_to, str) and len(pay_to) == 64 and req.get("asset") == x402.ASSET):
            raise x402.Invalid("invalid_payment_requirements", "requirements need asset PMX, payTo (64 hex), amount > 0")
        acc = payload.get("accepted")
        if not isinstance(acc, dict) or any(acc.get(k) != req.get(k) for k in ("scheme", "network", "asset", "payTo", "amount")):
            raise x402.Invalid("invalid_payment_requirements", "the payload's accepted entry differs from the requirements")
        method, inner = x402.parse(x402.encode(payload), self.network, pay_to, int(amount))
        memo = (req.get("extra") or {}).get("memo") if isinstance(req.get("extra"), dict) else None
        if method == "tx" and "transaction" in inner and memo is not None and inner["transaction"].get("memo") != memo:
            raise x402.Invalid("invalid_paymesh_challenge", "the tx memo is not the requirements' memo", 402)
        return method, inner, req

    def _txid_payment(self, txid, req):
        """A mined tx paying these requirements, or raise Invalid."""
        tx, _ = self.chain.find_tx_by_id(txid)
        if tx is None:
            raise x402.Invalid("invalid_transaction_state", "no mined tx with that id", 402)
        memo = (req.get("extra") or {}).get("memo")
        if tx.get("kind", "pay") != "pay" or tx["to_wallet"] != req["payTo"] or tx["amount"] != int(req["amount"]) \
                or (memo is not None and tx["memo"] != memo):
            raise x402.Invalid("invalid_paymesh_payment", "the tx does not pay these requirements", 402)
        return tx

    def _channel_header(self, inner):
        c = self.chain.ledger.channels.get(inner["channel"])
        if c is None or c["seller"] != self.hub_address:
            raise x402.Invalid("invalid_paymesh_channel_payment",
                               "this facilitator settles hub channels only (a direct channel is settled by its seller)", 402)
        return f"{inner['channel']} {inner['tick']} {inner['word']}", c

    # --- the endpoints ---
    def verify(self, body) -> dict:
        try:
            method, inner, req = self._parse(body)
            if method == "channel":
                header, c = self._channel_header(inner)
                ok, why = self.hub.accept(self.chain, header, int(req["amount"]), payee=req["payTo"], dry=True)
                if not ok:
                    raise x402.Invalid("invalid_paymesh_channel_payment", why, 402)
                return {"isValid": True, "payer": c["buyer"]}
            if "txid" in inner:
                tx = self._txid_payment(inner["txid"], req)
            else:
                tx = inner["transaction"]
                with self.lock:  # read-only: the ledger's own rules, nothing applied
                    self.chain.pledger.time = time.time()
                    try:
                        self.chain.pledger.check(tx)
                    except ValueError as e:
                        raise x402.Invalid("invalid_transaction_state", str(e), 402) from None
            if tx["txid"] in self.settled:
                raise x402.Invalid("invalid_transaction_state", "this payment was already settled", 402)
            return {"isValid": True, "payer": tx["from_wallet"]}
        except x402.Invalid as e:
            return {"isValid": False, "invalidReason": e.reason, "invalidMessage": str(e)[:200]}

    def settle(self, body) -> dict:
        net = self.network
        try:
            method, inner, req = self._parse(body)
        except x402.Invalid as e:
            return x402.settlement(net, False, reason=e.reason, message=str(e))
        amount = int(req["amount"])
        if method == "channel":
            try:
                header, c = self._channel_header(inner)
            except x402.Invalid as e:
                return x402.settlement(net, False, reason=e.reason, message=str(e))
            ok, why = self.hub.accept(self.chain, header, amount, payee=req["payTo"])
            if not ok:
                return x402.settlement(net, False, reason="invalid_paymesh_channel_payment", message=why)
            return x402.settlement(net, True, "", c["buyer"], amount)
        txid = inner["txid"] if "txid" in inner else inner["transaction"]["txid"]
        with self.mutex:  # one settle per payment at a time
            if txid in self.settled or txid in self.busy:
                return x402.settlement(net, False, reason="invalid_transaction_state",
                                       message="this payment was already settled")
            self.busy.add(txid)
        try:
            if "transaction" in inner:
                try:
                    self.submit(inner["transaction"])
                except x402.Pending as e:  # may still be mined: not settled, not refused
                    return x402.settlement(net, False, txid, reason="settlement_pending", message=str(e))
                except ValueError as e:
                    if "txid already used" not in str(e):  # a definite refusal
                        return x402.settlement(net, False, reason="invalid_transaction_state", message=str(e))
                    if self.chain.find_tx_by_id(txid)[0] is None:  # known but not yet mined
                        return x402.settlement(net, False, txid, reason="settlement_pending", message=x402.PENDING_MSG)
            try:
                tx = self._txid_payment(txid, req)
            except x402.Invalid as e:
                return x402.settlement(net, False, reason=e.reason, message=str(e))
            self.log.append(txid)  # durable before "success" goes out
            self.settled.add(txid)
            return x402.settlement(net, True, txid, tx["from_wallet"], amount)
        finally:
            with self.mutex:
                self.busy.discard(txid)
