"""Real encryption for private drafts.

Each person has one random key, which is what encrypts their drafts. That key is stored
only in encrypted form, and only their code can open it. The code itself is never stored,
so without the code the drafts cannot be read, even by someone holding the database.
"""
import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .accounts import stretch

MIN_LENGTH = 8
_KEY_LABEL = b"drafter-locker"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def wrap_key(key: bytes, code: str) -> str:
    salt, nonce = os.urandom(16), os.urandom(12)
    wrapped = AESGCM(stretch(code, salt)).encrypt(nonce, key, _KEY_LABEL)
    return json.dumps({"salt": _b64(salt), "nonce": _b64(nonce), "wrapped": _b64(wrapped)})


def new_locker(code: str) -> tuple[str, bytes]:
    key = AESGCM.generate_key(bit_length=256)
    return wrap_key(key, code), key


def open_locker(locker: str, code: str) -> bytes | None:
    """Returns the key, or None when the code is wrong."""
    record = json.loads(locker)
    salt, nonce, wrapped = (base64.b64decode(record[k]) for k in ("salt", "nonce", "wrapped"))
    try:
        return AESGCM(stretch(code, salt)).decrypt(nonce, wrapped, _KEY_LABEL)
    except InvalidTag:
        return None


def seal(key: bytes, run_id: str, content: dict) -> str:
    """The draft's id is mixed in, so encrypted text can't be swapped between drafts."""
    nonce = os.urandom(12)
    sealed = AESGCM(key).encrypt(nonce, json.dumps(content).encode(), run_id.encode())
    return json.dumps({"nonce": _b64(nonce), "sealed": _b64(sealed)})


def unseal(key: bytes, run_id: str, blob: str) -> dict:
    record = json.loads(blob)
    plain = AESGCM(key).decrypt(base64.b64decode(record["nonce"]), base64.b64decode(record["sealed"]), run_id.encode())
    return json.loads(plain)
