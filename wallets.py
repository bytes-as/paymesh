"""paymesh wallets: Lamport one-time signatures, pure hashlib (stdlib only).

Address = sha256 fingerprint of the wallet's public key, so a tx's pubkey is
bound to the from-address: sign with any other key and chain validation
rejects it. Each wallet file stores the 512-element secret key (256 pairs of
32-byte secrets); the public key is the sha256 of each element.

A Lamport signature reveals one element per message-hash bit; the verifier
hashes each reveal and compares against the public key. One key signs one
tx safely (POC scale: wallets can be rotated; the demo mints fresh wallets).

Stdlib only. Run directly for a self-test:
    py -3 wallets.py
"""
import hashlib
import json
import os
import secrets

WALLET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wallets")
BITS = 256  # one secret pair per bit of the sha256 digest


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def lamport_keygen():
    sk0 = [secrets.token_bytes(32) for _ in range(BITS)]
    sk1 = [secrets.token_bytes(32) for _ in range(BITS)]
    sk = [x.hex() for x in sk0] + [x.hex() for x in sk1]
    pub = [sha256_hex(x) for x in sk0] + [sha256_hex(x) for x in sk1]
    return sk, pub


def pubkey_fingerprint(pub: list) -> str:
    """The address a pubkey commits to."""
    return sha256_hex("".join(pub).encode())


def _msg_digest(tx: dict) -> bytes:
    msg = f"{tx['from_wallet']}|{tx['to_wallet']}|{tx['amount']}|{tx['memo']}|{tx['txid']}"
    return hashlib.sha256(msg.encode()).digest()


class Wallet:
    def __init__(self, name: str, sk: list, pub: list):
        self.name = name
        self.sk = sk
        self.pub = pub
        self.address = pubkey_fingerprint(pub)

    @property
    def path(self) -> str:
        return os.path.join(WALLET_DIR, f"{self.name}.wallet.json")

    def to_dict(self):
        return {"name": self.name, "v": 2, "address": self.address,
                "sk": self.sk, "pub": self.pub}

    def save(self):
        os.makedirs(WALLET_DIR, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f)


def create(name: str) -> Wallet:
    """Create and persist a fresh Lamport keypair. Fails if the name is taken."""
    if os.path.exists(os.path.join(WALLET_DIR, f"{name}.wallet.json")):
        raise FileExistsError(f"wallet '{name}' exists")
    sk, pub = lamport_keygen()
    w = Wallet(name, sk, pub)
    w.save()
    return w


def load(name: str) -> Wallet:
    path = os.path.join(WALLET_DIR, f"{name}.wallet.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"wallet '{name}' not found")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if raw.get("v") != 2:
        raise ValueError(f"wallet '{name}': pre-signature format, regenerate")
    return Wallet(raw["name"], raw["sk"], raw["pub"])


def load_or_create(name: str) -> Wallet:
    try:
        return load(name)
    except (FileNotFoundError, ValueError):
        try:
            return create(name)
        except FileExistsError:
            os.remove(os.path.join(WALLET_DIR, f"{name}.wallet.json"))
            return create(name)


def sign_tx(tx: dict, wallet: Wallet) -> dict:
    """Sign (from,to,amount,memo,txid) with the wallet's Lamport key, in place."""
    d = _msg_digest(tx)
    sig = []
    for i in range(BITS):
        bit = (d[i // 8] >> (i % 8)) & 1
        sig.append(wallet.sk[i if bit == 0 else BITS + i])
    tx["pub"] = wallet.pub
    tx["sig"] = sig
    return tx


def verify_tx(tx: dict) -> tuple[bool, str]:
    """True iff tx.pub commits to tx.from_wallet and tx.sig is a valid Lamport sig."""
    pub, sig = tx.get("pub"), tx.get("sig")
    if not pub or not sig:
        return False, "unsigned"
    if pubkey_fingerprint(pub) != tx["from_wallet"]:
        return False, "pubkey does not commit to from_wallet"
    if len(sig) != BITS:
        return False, "bad signature length"
    d = _msg_digest(tx)
    for i in range(BITS):
        bit = (d[i // 8] >> (i % 8)) & 1
        if sha256_hex(bytes.fromhex(sig[i])) != pub[i if bit == 0 else BITS + i]:
            return False, f"invalid signature at bit {i}"
    return True, "ok"


def fund(chain, name: str, amount: int) -> Wallet:
    """Mint to a wallet via an unsigned chain tx from the zero-address faucet."""
    w = load_or_create(name)
    chain.add_tx(chain.new_tx("0" * 64, w.address, amount, f"faucet:{name}"))
    return w


if __name__ == "__main__":
    import chain as ch

    selftest = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest-chain.json")
    if os.path.exists(selftest):
        os.remove(selftest)
    c = ch.Chain(selftest)
    c.genesis()
    w = fund(c, "selftest", 1000)
    c.mine()
    assert c.balance(w.address) == 1000
    w2 = load_or_create("selftest")
    assert w2.address == w.address

    # signed tx roundtrip
    tx = c.new_tx(w.address, "c" * 64, 10, "sig-test")
    sign_tx(tx, w)
    ok, why = verify_tx(tx)
    assert ok, why
    # flipped bit -> rejected
    bad = dict(tx, sig=tx["sig"][:128] + ["00" * 32] + tx["sig"][129:])
    ok2, why2 = verify_tx(bad)
    assert not ok2, why2
    # wrong key claiming w's address -> rejected
    thief = load_or_create("selftest-thief")
    forged = c.new_tx(w.address, "c" * 64, 10, "forge-test")
    sign_tx(forged, thief)
    ok3, why3 = verify_tx(forged)
    assert not ok3, why3
    # chain rejects unsigned/wrongly-signed txs at add_tx
    for t in (dict(tx, sig=None), forged):
        try:
            c.add_tx(t)
            raise AssertionError("add_tx accepted a bad tx")
        except ValueError:
            pass
    print(f"self-test ok: wallet {w.name} {w.address[:16]}... balance {c.balance(w.address)}; "
          f"sig valid, tamper/forgery/unsigned rejected")
