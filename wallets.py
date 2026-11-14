"""paymesh wallets: Lamport one-time signatures with key rotation (stdlib only).

A Lamport key is safe for ONE signature: each signature publishes half of the
secret key, and two signatures with one key publish enough for anyone to forge
a third. So every wallet is a chain of one-time keys:

* key i is derived from the wallet's 32-byte seed: sk[j] = sha256(seed|i|j);
* the wallet's address is the fingerprint of key 0;
* every signed tx carries `next_key`, the fingerprint of the key that must
  sign the wallet's next tx, and the signature covers it;
* the ledger (chain.py) remembers each address's current key and refuses a
  tx signed by any other key, so a key that has signed once never verifies
  again and published secrets are useless.

Wallet files hold only the seed (keep them secret) and a hint of the next key
index. Run directly for a self-test:
    py -3 wallets.py
"""
import hashlib
import hmac
import json
import os
import re
import secrets

# PAYMESH_HOME holds chain.json and wallets/ (default: this folder)
HOME = os.environ.get("PAYMESH_HOME") or os.path.dirname(os.path.abspath(__file__))
WALLET_DIR = os.path.join(HOME, "wallets")
BITS = 256  # Lamport: one secret pair per bit of the sha256 digest
# Winternitz (WOTS, w=16): 64 message digits of 4 bits + 3 checksum digits,
# each a hash chain of 15 steps. 67 x 32 bytes per signature instead of
# Lamport's 768 x 32, and no public key on the wire: the verifier rebuilds it.
WOTS_W, WOTS_L = 16, 67
VERSION = 4
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_KEY_SEARCH = 20_000


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --- seeds at rest: scrypt -> (enc key, mac key); HMAC-SHA256 counter keystream; encrypt-then-MAC ---
SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def _keys(passphrase: str, salt: bytes, kdf: dict):
    k = hashlib.scrypt(passphrase.encode(), salt=salt, n=kdf["n"], r=kdf["r"], p=kdf["p"], dklen=64, maxmem=2**26)
    return k[:32], k[32:]


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = b""
    for ctr in range((n + 31) // 32):
        out += hmac.new(key, nonce + ctr.to_bytes(8, "big"), hashlib.sha256).digest()
    return out[:n]


def seal_seed(seed: bytes, passphrase: str) -> dict:
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(16)
    ek, mk = _keys(passphrase, salt, SCRYPT)
    ct = bytes(a ^ b for a, b in zip(seed, _stream(ek, nonce, len(seed))))
    mac = hmac.new(mk, salt + nonce + ct, hashlib.sha256).hexdigest()
    return {"kdf": "scrypt", **SCRYPT, "salt": salt.hex(), "nonce": nonce.hex(), "ct": ct.hex(), "mac": mac}


def open_seed(box: dict, passphrase: str | None) -> bytes:
    if not passphrase:
        raise PermissionError("this wallet is encrypted: set PAYMESH_PASSPHRASE")
    salt, nonce, ct = bytes.fromhex(box["salt"]), bytes.fromhex(box["nonce"]), bytes.fromhex(box["ct"])
    ek, mk = _keys(passphrase, salt, box)
    if not hmac.compare_digest(hmac.new(mk, salt + nonce + ct, hashlib.sha256).hexdigest(), box["mac"]):
        raise PermissionError("wrong passphrase (or the wallet file was changed)")
    return bytes(a ^ b for a, b in zip(ct, _stream(ek, nonce, len(ct))))


def check_name(name) -> str:
    """Wallet names become file names: letters, digits, _ and - only."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError(f"wallet name {name!r}: use 1-32 letters, digits, _ or -")
    return name


def derive_key(seed: bytes, i: int):
    """One-time key i of a wallet: (secret key, public key), 2*BITS elements each."""
    base = seed + i.to_bytes(8, "big")
    sk = [hashlib.sha256(base + j.to_bytes(2, "big")).digest() for j in range(2 * BITS)]
    return [x.hex() for x in sk], [sha256_hex(x) for x in sk]


def _chain(x: bytes, steps: int) -> bytes:
    for _ in range(steps):
        x = hashlib.sha256(x).digest()
    return x


def _wots_digits(d: bytes) -> list:
    """64 base-16 digits of the digest plus a 3-digit checksum: raising any digit
    (hashing a revealed chain further) must lower the checksum, which can't be undone."""
    digits = []
    for byte in d:
        digits += [byte >> 4, byte & 15]
    csum = sum(WOTS_W - 1 - x for x in digits)  # at most 960 < 16**3
    return digits + [(csum >> 8) & 15, (csum >> 4) & 15, csum & 15]


def derive_wots(seed: bytes, i: int):
    """WOTS key i: (secret chain starts, public chain ends), 67 x 32 bytes each."""
    base = seed + b"wots" + i.to_bytes(8, "big")
    sk = [hashlib.sha256(base + j.to_bytes(2, "big")).digest() for j in range(WOTS_L)]
    return sk, [_chain(x, WOTS_W - 1) for x in sk]


def wots_fingerprint(pk: list) -> str:
    return sha256_hex(b"wots1" + b"".join(pk))


def pubkey_fingerprint(pub: list) -> str:
    """The 64-hex id of a public key (key 0's is the wallet address)."""
    return sha256_hex("".join(pub).encode())


EXTRA_FIELDS = ("kind", "anchor", "ticks", "unit", "expires", "channel", "word")


def _msg_digest(tx: dict) -> bytes:
    msg = f"{tx['from_wallet']}|{tx['to_wallet']}|{tx['amount']}|{tx['memo']}|{tx['txid']}|{tx.get('next_key', '')}"
    extra = {k: tx[k] for k in EXTRA_FIELDS if k in tx}
    if extra:  # channel txs: the signature covers their terms too
        msg += "|" + json.dumps(extra, sort_keys=True)
    return hashlib.sha256(msg.encode()).digest()


def _bit(d: bytes, i: int) -> int:
    return (d[i // 8] >> (i % 8)) & 1


class Wallet:
    """A chain of one-time keys. Keys from index `wots_from` on are WOTS, earlier
    ones Lamport (wallets made before WOTS switch at their next key)."""

    def __init__(self, name: str, seed: bytes, next_index: int = 0, wots_from: int | None = 0):
        self.name = check_name(name)
        self.seed = seed
        self.next_index = next_index
        self.wots_from = wots_from
        self.sealed: dict | None = None  # the encrypted seed, rewritten as is on save
        self._cache: dict = {}
        self.address = self.key_fp(0)

    def uses_wots(self, i: int) -> bool:
        return self.wots_from is not None and i >= self.wots_from

    def key(self, i: int):
        k = ("w" if self.uses_wots(i) else "l", i)
        if k not in self._cache:
            if len(self._cache) > 8:
                self._cache.clear()
            self._cache[k] = derive_wots(self.seed, i) if k[0] == "w" else derive_key(self.seed, i)
        return self._cache[k]

    def key_fp(self, i: int) -> str:
        pub = self.key(i)[1]
        return wots_fingerprint(pub) if self.uses_wots(i) else pubkey_fingerprint(pub)

    def index_of(self, fp: str) -> int:
        """Which of this wallet's keys has fingerprint fp, searching outward from the hint.

        The hint runs ahead of the ledger when a signed tx was refused (the key
        it used is still current), so look back as well as forward, nearest first.
        """
        start = self.next_index
        for d in range(MAX_KEY_SEARCH):
            for i in (start - d, start + d) if d else (start,):
                if i >= 0 and self.key_fp(i) == fp:
                    return i
        raise ValueError(f"wallet {self.name}: no key matches the ledger's current key")

    @property
    def path(self) -> str:
        return os.path.join(WALLET_DIR, f"{self.name}.wallet.json")

    def to_dict(self):
        d = {"name": self.name, "v": VERSION, "address": self.address,
             "next_index": self.next_index, "wots_from": self.wots_from}
        if self.sealed:
            d["seed_enc"] = self.sealed
        else:
            d["seed"] = self.seed.hex()
        return d

    def save(self):
        os.makedirs(WALLET_DIR, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f)
        os.replace(tmp, self.path)


def _path(name: str) -> str:
    return os.path.join(WALLET_DIR, f"{check_name(name)}.wallet.json")


def create(name: str, passphrase: str | None = None) -> Wallet:
    """Create and persist a fresh wallet (its seed encrypted when a passphrase is given)."""
    if os.path.exists(_path(name)):
        raise FileExistsError(f"wallet '{name}' exists")
    w = Wallet(name, secrets.token_bytes(32))
    if passphrase:
        w.sealed = seal_seed(w.seed, passphrase)
    w.save()
    return w


def load(name: str) -> Wallet:
    path = _path(name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"wallet '{name}' not found")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if raw.get("v") not in (3, VERSION):
        raise LookupError(f"wallet '{name}': old format v{raw.get('v')}")
    # v3 wallets were all-Lamport: wots_from None until their next signature
    wf = raw.get("wots_from") if raw.get("v") == VERSION else None
    if "seed_enc" in raw:
        seed = open_seed(raw["seed_enc"], os.environ.get("PAYMESH_PASSPHRASE"))
    else:
        seed = bytes.fromhex(raw["seed"])
    w = Wallet(raw["name"], seed, int(raw.get("next_index", 0)), wf)
    w.sealed = raw.get("seed_enc")
    if w.address != raw.get("address", w.address):
        raise PermissionError(f"wallet '{name}': the seed does not match the address")
    return w


def load_or_create(name: str) -> Wallet:
    """Load a wallet, or create it. An old-format file is moved aside, never deleted."""
    try:
        return load(name)
    except FileNotFoundError:
        return create(name)
    except LookupError:
        legacy = os.path.join(WALLET_DIR, "legacy")
        os.makedirs(legacy, exist_ok=True)
        os.replace(_path(name), os.path.join(legacy, os.path.basename(_path(name))))
        return create(name)


def sign_tx(tx: dict, wallet: Wallet, ledger) -> dict:
    """Sign tx with the key the ledger expects for this wallet, and commit to the next one.

    `ledger` is the chain (anything with current_key(address)). In place; returns tx.
    """
    i = wallet.index_of(ledger.current_key(wallet.address))
    if wallet.wots_from is None:
        wallet.wots_from = i + 1  # a Lamport-era wallet: every key after this one is WOTS
    sk, pub = wallet.key(i)
    tx["next_key"] = wallet.key_fp(i + 1)
    d = _msg_digest(tx)
    if wallet.uses_wots(i):
        tx.pop("pub", None)
        digits = _wots_digits(d)
        tx["sig"] = [_chain(sk[j], digits[j]).hex() for j in range(WOTS_L)]
    else:
        tx["pub"] = pub
        tx["sig"] = [sk[i2 if _bit(d, i2) == 0 else BITS + i2] for i2 in range(BITS)]
    wallet.next_index = i + 1
    if os.path.exists(wallet.path):
        wallet.save()
    return tx


def verify_tx(tx: dict, expected_key: str) -> tuple[bool, str]:
    """True iff tx is signed by the key with fingerprint expected_key and commits to a next key."""
    pub, sig = tx.get("pub"), tx.get("sig")
    if not sig:
        return False, "unsigned"
    nk = tx.get("next_key")
    if not isinstance(nk, str) or not HEX64.match(nk) or nk == expected_key:
        return False, "next_key must be a new 64-hex key fingerprint"
    if pub is None:  # WOTS: finish each chain and compare the rebuilt key's fingerprint
        if not isinstance(sig, list) or len(sig) != WOTS_L or not all(isinstance(x, str) and HEX64.match(x) for x in sig):
            return False, "bad signature shape"
        digits = _wots_digits(_msg_digest(tx))
        pk = [_chain(bytes.fromhex(sig[j]), WOTS_W - 1 - digits[j]) for j in range(WOTS_L)]
        if wots_fingerprint(pk) != expected_key:
            return False, "not signed by this wallet's current key (keys are one-time)"
        return True, "ok"
    if not isinstance(pub, list) or len(pub) != 2 * BITS or not isinstance(sig, list) or len(sig) != BITS:
        return False, "bad signature shape"
    if not all(isinstance(x, str) and HEX64.match(x) for x in pub + sig):
        return False, "bad signature encoding"
    if pubkey_fingerprint(pub) != expected_key:
        return False, "not signed by this wallet's current key (keys are one-time)"
    d = _msg_digest(tx)
    for i in range(BITS):
        if sha256_hex(bytes.fromhex(sig[i])) != pub[i if _bit(d, i) == 0 else BITS + i]:
            return False, f"invalid signature at bit {i}"
    return True, "ok"


# --- helpers for the attack tests: what a forger sees and controls ---
def signer_key(tx: dict) -> str:
    """Fingerprint of the key that signed tx (Lamport or WOTS)."""
    if tx.get("pub") is not None:
        return pubkey_fingerprint(tx["pub"])
    digits = _wots_digits(_msg_digest(tx))
    return wots_fingerprint([_chain(bytes.fromhex(tx["sig"][j]), WOTS_W - 1 - digits[j]) for j in range(WOTS_L)])


def wots_forge(tx: dict, revealed: list) -> list | None:
    """What a forger can build for tx from WOTS signatures already published by
    one key: chain j can only be hashed FORWARD from a revealed value."""
    digits = _wots_digits(_msg_digest(tx))
    out = []
    for j in range(WOTS_L):
        best = None
        for d_rev, s in revealed:
            if d_rev[j] <= digits[j] and (best is None or d_rev[j] > best[0]):
                best = (d_rev[j], s[j])
        if best is None:
            return None
        out.append(_chain(bytes.fromhex(best[1]), digits[j] - best[0]).hex())
    return out


def forge_fields(ledger, address: str) -> dict:
    """Fields a forger picks freely: any next_key they like."""
    return {"next_key": "e" * 64}


def fund(chain, name: str, amount: int) -> Wallet:
    """Mint to a wallet via an unsigned tx from the zero-address faucet (amount 0: just create)."""
    w = load_or_create(name)
    while amount > 0:  # the ledger caps one mint at 100 PMX
        part = min(amount, 100)
        chain.add_tx(chain.new_tx("0" * 64, w.address, part, f"faucet:{name}"))
        amount -= part
    return w


if __name__ == "__main__":
    import tempfile
    import chain as ch

    tmp = tempfile.mkdtemp()
    WALLET_DIR = os.path.join(tmp, "wallets")
    c = ch.Chain(os.path.join(tmp, "chain.json"))
    c.genesis()
    w = fund(c, "selftest", 1000)
    c.mine()
    assert c.balance(w.address) == 1000
    keys = set()
    for n in range(3):
        tx = c.new_tx(w.address, "c" * 64, 10, f"sig-test-{n}")
        sign_tx(tx, w, c)
        keys.add(signer_key(tx))
        c.add_tx(tx)
        c.mine()
    assert len(keys) == 3, "a key signed twice"
    assert c.balance(w.address) == 970
    old = c.all_txs()[-1]
    replay = dict(old, txid="d" * 64)  # same key again, new txid
    try:
        c.add_tx(replay)
        raise AssertionError("a spent key verified again")
    except ValueError:
        pass
    print(f"self-test ok: 3 txs signed by 3 different one-time keys; balance {c.balance(w.address)}; spent key refused")
