"""Salted password hashing for access groups, using only the standard library.

A group's password is never stored in clear text — only a salted PBKDF2-HMAC-
SHA256 digest. The stored string is self-describing so the parameters travel
with the hash and can be bumped over time without breaking existing rows:

    pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>

``verify_password`` re-derives with the *stored* iteration count/salt and
compares in constant time, so raising :data:`ITERATIONS` only affects newly
created/changed passwords. We deliberately avoid a third-party dependency
(passlib/bcrypt): PBKDF2 is in ``hashlib`` and is more than adequate for gating
shared test data.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

_ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 240_000
_SALT_BYTES = 16


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    """Return a self-describing salted hash for ``password``.

    A random salt is generated per call, so the same password hashes differently
    each time. Raises :class:`ValueError` for an empty password — a group with a
    blank password could be "unlocked" by anyone and is never intended.
    """

    if not password:
        raise ValueError("Пароль не может быть пустым")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, iterations)
    return f"{_ALGORITHM}${iterations}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """True iff ``password`` matches the PBKDF2 hash in ``stored``.

    Tolerant of malformed/blank inputs (returns ``False`` rather than raising),
    so a corrupt row or empty candidate simply fails to unlock.
    """

    if not password or not stored:
        return False
    try:
        algorithm, iter_str, salt_b64, hash_b64 = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iter_str)
        salt = _b64decode(salt_b64)
        expected = _b64decode(hash_b64)
    except (ValueError, binascii.Error):
        return False
    candidate = _derive(password, salt, iterations)
    return hmac.compare_digest(candidate, expected)
