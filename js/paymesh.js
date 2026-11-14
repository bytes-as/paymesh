/* paymesh.js: a dependency-free browser (and Node) client that pays paymesh x402 routes.
 *
 * One file, no build step. Signs on-chain payments with WOTS v2 one-time keys, byte for byte
 * as wallets.py does (cross-tested in tests/test_js_client.py), and pays a route the x402 v2
 * way: GET -> 402 with PAYMENT-REQUIRED -> GET again with PAYMENT-SIGNATURE -> 200 with
 * PAYMENT-RESPONSE. SHA-256 is pure JS, so it also runs on plain-http pages (no crypto.subtle).
 *
 *   const w = Paymesh.Wallet.create();          // or Paymesh.Wallet.fromJSON(saved)
 *   const api = new Paymesh.Client("http://127.0.0.1:8402", w);
 *   const r = await api.get("/premium/price");  // {status, body, receipt, paid}
 *
 * Keys: a browser page can't read /account or post to /tx (paymesh refuses other sites there,
 * PROTOCOL 0c), so the wallet tracks its key index itself: 0 for a new wallet, +1 after each
 * payment that was settled or is pending. A payment refused after signing exposed that one-time
 * key: the wallet locks instead of signing anything else with it (retire it from a script:
 * `py -3 wallet.py`), the same rule the Python client follows with its retire-key payment.
 * The seed is the money: keep it like a password.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.Paymesh = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // --- SHA-256 (FIPS 180-4) ---------------------------------------------------------------
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);
  const W = new Uint32Array(64);

  function sha256(data) {
    const len = data.length;
    const n = ((len + 9 + 63) >>> 6) << 6;
    const m = new Uint8Array(n);
    m.set(data);
    m[len] = 0x80;
    const dv = new DataView(m.buffer);
    dv.setUint32(n - 8, Math.floor(len / 0x20000000));
    dv.setUint32(n - 4, (len << 3) >>> 0);
    let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a,
      h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
    for (let off = 0; off < n; off += 64) {
      for (let i = 0; i < 16; i++) W[i] = dv.getUint32(off + i * 4);
      for (let i = 16; i < 64; i++) {
        const x = W[i - 15], y = W[i - 2];
        const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
        const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
        W[i] = (W[i - 16] + s0 + W[i - 7] + s1) | 0;
      }
      let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
      for (let i = 0; i < 64; i++) {
        const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        const t1 = (h + S1 + ((e & f) ^ (~e & g)) + K[i] + W[i]) | 0;
        const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        const t2 = (S0 + ((a & b) ^ (a & c) ^ (b & c))) | 0;
        h = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      h0 = (h0 + a) | 0; h1 = (h1 + b) | 0; h2 = (h2 + c) | 0; h3 = (h3 + d) | 0;
      h4 = (h4 + e) | 0; h5 = (h5 + f) | 0; h6 = (h6 + g) | 0; h7 = (h7 + h) | 0;
    }
    const out = new Uint8Array(32), ov = new DataView(out.buffer);
    [h0, h1, h2, h3, h4, h5, h6, h7].forEach((v, i) => ov.setUint32(i * 4, v >>> 0));
    return out;
  }

  // --- bytes ---------------------------------------------------------------------------------
  const enc = new TextEncoder();
  const utf8 = (s) => enc.encode(s);
  const hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  function unhex(s) {
    if (!/^([0-9a-f]{2})*$/.test(s)) throw new Error("bad hex");
    const b = new Uint8Array(s.length / 2);
    for (let i = 0; i < b.length; i++) b[i] = parseInt(s.substr(i * 2, 2), 16);
    return b;
  }
  function cat(...parts) {
    const n = parts.reduce((k, p) => k + p.length, 0), out = new Uint8Array(n);
    let o = 0;
    for (const p of parts) { out.set(p, o); o += p.length; }
    return out;
  }
  function be(v, bytes) {
    const out = new Uint8Array(bytes);
    for (let i = bytes - 1; i >= 0; i--) { out[i] = v % 256; v = Math.floor(v / 256); }
    return out;
  }
  const sha256hex = (b) => hex(sha256(b));
  function randomBytes(n) {
    const b = new Uint8Array(n);
    globalThis.crypto.getRandomValues(b);
    return b;
  }

  // --- JSON exactly as Python's json.dumps(obj, sort_keys=True) (ensure_ascii) ----------------
  function pyStr(s) {
    let out = '"';
    for (let i = 0; i < s.length; i++) {
      const c = s.charCodeAt(i);
      if (c === 0x22) out += '\\"';
      else if (c === 0x5c) out += "\\\\";
      else if (c === 0x0a) out += "\\n";
      else if (c === 0x0d) out += "\\r";
      else if (c === 0x09) out += "\\t";
      else if (c === 0x08) out += "\\b";
      else if (c === 0x0c) out += "\\f";
      else if (c < 0x20 || c > 0x7e) out += "\\u" + c.toString(16).padStart(4, "0");
      else out += s[i];
    }
    return out + '"';
  }
  function pyJson(v, compact) {
    const cs = compact ? "," : ", ", ks = compact ? ":" : ": ";
    if (v === null) return "null";
    if (v === true) return "true";
    if (v === false) return "false";
    if (typeof v === "number") {
      if (!Number.isSafeInteger(v)) throw new Error("only integers are signed: " + v);
      return String(v);
    }
    if (typeof v === "string") return pyStr(v);
    if (Array.isArray(v)) return "[" + v.map((x) => pyJson(x, compact)).join(cs) + "]";
    if (typeof v === "object") {
      return "{" + Object.keys(v).filter((k) => v[k] !== undefined).sort()
        .map((k) => pyStr(k) + ks + pyJson(v[k], compact)).join(cs) + "}";
    }
    throw new Error("can't encode " + typeof v);
  }

  // --- WOTS v2 (wallets.py: derive_wots2, _chain2, _wots_digits, _digest2) -------------------
  const WOTS_W = 16, WOTS_L = 67;
  const EXTRA_FIELDS = ["kind", "anchor", "ticks", "unit", "expires", "channel", "word", "splits", "fee"];
  const FEE_BYTES = 8192;

  function chain2(pkSeed, j, x, start, steps) {
    for (let step = start; step < start + steps; step++) {
      x = sha256(cat(pkSeed, Uint8Array.of(0x02, j, step), x));
    }
    return x;
  }
  function deriveWots2(seed, i) {
    const n = be(i, 8);
    const pkSeed = sha256(cat(seed, utf8("wots2-pub"), n));
    const sk = [], pk = [];
    for (let j = 0; j < WOTS_L; j++) {
      sk.push(sha256(cat(seed, utf8("wots2"), n, be(j, 2))));
      pk.push(chain2(pkSeed, j, sk[j], 0, WOTS_W - 1));
    }
    return { sk, pk, pkSeed };
  }
  const wots2Fingerprint = (pkSeed, pk) => sha256hex(cat(utf8("wots2"), pkSeed, ...pk));
  function wotsDigits(d) {
    const digits = [];
    for (const byte of d) digits.push(byte >> 4, byte & 15);
    const csum = digits.reduce((s, x) => s + (WOTS_W - 1 - x), 0);
    return digits.concat([(csum >> 8) & 15, (csum >> 4) & 15, csum & 15]);
  }
  function msgDigest(tx) {
    let msg = `${tx.from_wallet}|${tx.to_wallet}|${tx.amount}|${tx.memo}|${tx.txid}|${tx.next_key || ""}`;
    const extra = {};
    for (const k of EXTRA_FIELDS) if (k in tx) extra[k] = tx[k];
    if (Object.keys(extra).length) msg += "|" + pyJson(extra, false);
    return sha256(utf8(msg));
  }
  const txSize = (tx) => pyJson(tx, true).length; // ASCII only (ensure_ascii): chars == bytes
  function setFee(tx) {
    const zero = "0".repeat(64);
    let fee = 1;
    for (let k = 0; k < 4; k++) {
      tx.fee = fee;
      const need = Math.max(1, Math.ceil(txSize(Object.assign({}, tx, { next_key: zero, sig: Array(WOTS_L + 1).fill(zero) })) / FEE_BYTES));
      if (need === fee) break;
      fee = need;
    }
    tx.fee = fee;
    return fee;
  }

  // --- wallet -----------------------------------------------------------------------------
  class Wallet {
    constructor(seed, nextIndex = 0, locked = null) {
      if (!(seed instanceof Uint8Array) || seed.length !== 32) throw new Error("seed must be 32 bytes");
      this.seed = seed;
      this.nextIndex = nextIndex;
      this.locked = locked; // index of a key a refused payment exposed
      this._cache = new Map();
      this.address = this.keyFingerprint(0);
    }
    static create() { return new Wallet(randomBytes(32)); }
    /** A saved browser wallet, or a Python wallet file (format 5, plain seed, WOTS v2 from key 0). */
    static fromJSON(o) {
      if (o.seed_enc) throw new Error("passphrase-sealed wallets can't be opened here");
      if (o.v !== undefined && !(o.v === 5 && o.wots_from === 0 && o.wots2_from === 0)) {
        throw new Error("only wallets that sign with WOTS v2 from key 0 (format 5) can be used here");
      }
      const w = new Wallet(unhex(o.seed), o.next_index || 0, o.locked ?? null);
      if (o.address && o.address !== w.address) throw new Error("the seed does not match the address");
      return w;
    }
    toJSON() {
      return { seed: hex(this.seed), address: this.address, next_index: this.nextIndex, locked: this.locked };
    }
    key(i) {
      if (!this._cache.has(i)) {
        if (this._cache.size > 8) this._cache.clear();
        this._cache.set(i, deriveWots2(this.seed, i));
      }
      return this._cache.get(i);
    }
    keyFingerprint(i) {
      const k = this.key(i);
      return wots2Fingerprint(k.pkSeed, k.pk);
    }
  }

  function newTx(from, to, amount, memo) {
    const txid = sha256hex(utf8(`${from}${to}${amount}${memo}${Date.now()}${hex(randomBytes(8))}`));
    return { from_wallet: from, to_wallet: to, amount, memo, txid };
  }

  /** Sign tx in place with key i of wallet (wallets.sign_tx for a WOTS v2 key). */
  function signTx(tx, wallet, i) {
    if (!("fee" in tx)) setFee(tx);
    tx.next_key = wallet.keyFingerprint(i + 1);
    const { sk, pkSeed } = wallet.key(i);
    const digits = wotsDigits(sha256(cat(utf8("paymesh-wots2-msg"), pkSeed, msgDigest(tx))));
    tx.sig = [hex(pkSeed)].concat(sk.map((s, j) => hex(chain2(pkSeed, j, s, 0, digits[j]))));
    return tx;
  }

  // --- x402 -------------------------------------------------------------------------------
  const encode = (obj) => btoa(pyJson(obj, true));
  function decode(v) {
    if (!v) return null;
    const bin = atob(v), b = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) b[i] = bin.charCodeAt(i);
    return JSON.parse(new TextDecoder().decode(b));
  }

  class PaymentError extends Error {}

  class Client {
    /** api: the paid API's base URL. wallet: a Wallet. onSave(wallet): called after a key is used. */
    constructor(api, wallet, onSave) {
      this.api = api.replace(/\/+$/, "");
      this.wallet = wallet;
      this.onSave = onSave || (() => {});
    }
    async _fetch(path, headers) {
      const r = await fetch(this.api + path, { headers: headers || {}, credentials: "omit" });
      const text = await r.text();
      let body;
      try { body = JSON.parse(text || "null"); } catch (e) { body = { text }; }
      return { status: r.status, body, headers: r.headers };
    }
    /** GET path; pay on a 402 with an on-chain tx signed here. */
    async get(path) {
      let r = await this._fetch(path);
      if (r.status !== 402) return { status: r.status, body: r.body, receipt: decode(r.headers.get("PAYMENT-RESPONSE")), paid: false };
      const required = decode(r.headers.get("PAYMENT-REQUIRED"));
      if (!required) throw new PaymentError("402 without a readable PAYMENT-REQUIRED header (the server must expose it to this origin)");
      const accepted = (required.accepts || []).find((a) => a.scheme === "paymesh" && a.extra && a.extra.assetTransferMethod === "tx");
      if (!accepted) throw new PaymentError("the 402 offers no paymesh tx payment");
      const w = this.wallet;
      if (w.locked !== null) {
        throw new PaymentError(`key ${w.locked} was seen in a refused payment and must not sign again: retire it from a script (py -3 wallet.py) or use a new wallet`);
      }
      const i = w.nextIndex;
      const tx = signTx(newTx(w.address, accepted.payTo, Number(accepted.amount), accepted.extra.memo), w, i);
      r = await this._fetch(path, { "PAYMENT-SIGNATURE": encode({ x402Version: 2, accepted, payload: { transaction: tx } }) });
      const receipt = decode(r.headers.get("PAYMENT-RESPONSE"));
      if ((r.status === 200 && receipt && receipt.success) || (receipt && receipt.errorReason === "settlement_pending")) {
        w.nextIndex = i + 1; // mined (or queued to be): the ledger now expects the next key
      } else {
        w.locked = i;
      }
      this.onSave(w);
      return { status: r.status, body: r.body, receipt, paid: r.status === 200, tx };
    }
  }

  return { sha256, sha256hex, hex, unhex, utf8, pyJson, deriveWots2, wots2Fingerprint, wotsDigits, msgDigest,
    setFee, txSize, newTx, signTx, encode, decode, Wallet, Client, PaymentError, WOTS_L, VERSION: 2 };
});
