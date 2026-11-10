"""Passwords, log-in sessions, and slowing down people who guess."""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from . import db

MIN_PASSWORD = 8
SESSION_DAYS = 30
MAX_WRONG = 5
WAIT_SECONDS = 30

# Slow on purpose, and needs a lot of memory, so guessing in bulk is expensive.
_SCRYPT = {"n": 2**15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}


class TooManyTries(Exception):
    pass


class Tries:
    """After a few wrong answers, the next ones have to wait."""

    def __init__(self):
        self._guard = threading.Lock()
        self._wrong: dict[str, int] = {}
        self._blocked: dict[str, float] = {}

    def check(self, key: str) -> None:
        with self._guard:
            if time.time() < self._blocked.get(key, 0):
                raise TooManyTries

    def failed(self, key: str) -> None:
        with self._guard:
            if len(self._wrong) > 5000:  # made-up names must not fill the memory
                self._wrong.clear()
            self._wrong[key] = self._wrong.get(key, 0) + 1
            if self._wrong[key] >= MAX_WRONG:
                self._wrong.pop(key)
                self._blocked[key] = time.time() + WAIT_SECONDS

    def passed(self, key: str) -> None:
        with self._guard:
            self._wrong.pop(key, None)
            self._blocked.pop(key, None)

    def reset(self) -> None:
        with self._guard:
            self._wrong.clear()
            self._blocked.clear()


tries = Tries()


def stretch(secret: str, salt: bytes) -> bytes:
    return hashlib.scrypt(secret.encode(), salt=salt, **_SCRYPT)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    return json.dumps({"salt": salt.hex(), "hash": stretch(password, salt).hex()})


_NOBODY = hash_password(secrets.token_hex(16))


def password_matches(stored: str | None, password: str) -> bool:
    """Unknown names take the same time, so nobody can tell which names exist."""
    record = json.loads(stored or _NOBODY)
    right = hmac.compare_digest(stretch(password, bytes.fromhex(record["salt"])).hex(), record["hash"])
    return right and stored is not None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    db.add_session(hash_token(token), user_id, time.time() + SESSION_DAYS * 86400)
    return token
