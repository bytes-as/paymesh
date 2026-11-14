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
import json
import os
import re
import secrets

# PAYMESH_HOME holds chain.json and wallets/ (default: this folder)
HOME = os.environ.get("PAYMESH_HOME") or os.path.dirname(os.path.abspath(__file__))
WALLET_DIR = os.path.join(HOME, "wallets")
BITS = 256  # one secret pair per bit of the sha256 digest
VERSION = 3
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_KEY_SEARCH = 20_000


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


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
    def __init__(self, name: str, seed: bytes, next_index: int = 0):
        self.name = check_name(name)
        self.seed = seed
        self.next_index = next_index
        self._cache: dict = {}
        self.address = self.key_fp(0)

    def key(self, i: int):
        if i not in self._cache:
            if len(self._cache) > 8:
                self._cache.clear()
            self._cache[i] = derive_key(self.seed, i)
        return self._cache[i]

    def key_fp(self, i: int) -> str:
        return pubkey_fingerprint(self.key(i)[1])

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
        return {"name": self.name, "v": VERSION, "address": self.address,
                "seed": self.seed.hex(), "next_index": self.next_index}

    def save(self):
        os.makedirs(WALLET_DIR, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f)
        os.replace(tmp, self.path)


def _path(name: str) -> str:
    return os.path.join(WALLET_DIR, f"{check_name(name)}.wallet.json")


def create(name: str) -> Wallet:
    """Create and persist a fresh wallet. Fails if the name is taken."""
    if os.path.exists(_path(name)):
        raise FileExistsError(f"wallet '{name}' exists")
    w = Wallet(name, secrets.token_bytes(32))
    w.save()
    return w


def load(name: str) -> Wallet:
    path = _path(name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"wallet '{name}' not found")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if raw.get("v") != VERSION:
        raise LookupError(f"wallet '{name}': old format v{raw.get('v')}")
    return Wallet(raw["name"], bytes.fromhex(raw["seed"]), int(raw.get("next_index", 0)))


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
    sk, pub = wallet.key(i)
    tx["next_key"] = wallet.key_fp(i + 1)
    d = _msg_digest(tx)
    tx["pub"] = pub
    tx["sig"] = [sk[i2 if _bit(d, i2) == 0 else BITS + i2] for i2 in range(BITS)]
    wallet.next_index = i + 1
    if os.path.exists(wallet.path):
        wallet.save()
    return tx


def verify_tx(tx: dict, expected_key: str) -> tuple[bool, str]:
    """True iff tx is signed by the key with fingerprint expected_key and commits to a next key."""
    pub, sig = tx.get("pub"), tx.get("sig")
    if not pub or not sig:
        return False, "unsigned"
    if not isinstance(pub, list) or len(pub) != 2 * BITS or not isinstance(sig, list) or len(sig) != BITS:
        return False, "bad signature shape"
    if not all(isinstance(x, str) and HEX64.match(x) for x in pub + sig):
        return False, "bad signature encoding"
    if pubkey_fingerprint(pub) != expected_key:
        return False, "not signed by this wallet's current key (keys are one-time)"
    nk = tx.get("next_key")
    if not isinstance(nk, str) or not HEX64.match(nk) or nk == expected_key:
        return False, "next_key must be a new 64-hex key fingerprint"
    d = _msg_digest(tx)
    for i in range(BITS):
        if sha256_hex(bytes.fromhex(sig[i])) != pub[i if _bit(d, i) == 0 else BITS + i]:
            return False, f"invalid signature at bit {i}"
    return True, "ok"


# --- helpers for the attack tests: what a forger sees and controls ---
def full_pub(tx: dict) -> list:
    return tx["pub"]


def forge_fields(ledger, address: str) -> dict:
    """Fields a forger picks freely: any next_key they like."""
    return {"next_key": "e" * 64}


def fund(chain, name: str, amount: int) -> Wallet:
    """Mint to a wallet via an unsigned tx from the zero-address faucet (amount 0: just create)."""
    w = load_or_create(name)
    if amount > 0:
        chain.add_tx(chain.new_tx("0" * 64, w.address, amount, f"faucet:{name}"))
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
        keys.add(pubkey_fingerprint(tx["pub"]))
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
