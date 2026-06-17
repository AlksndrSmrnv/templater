"""Unlocked access groups, carried in a server-issued HMAC cookie.

The app has no user accounts by design (see :mod:`app.utils.edit_mode` for the
sibling pattern that gates settings editing). To view a group's private test
data a user enters the group's password; on success the server records the
group as "unlocked" in a signed, time-limited cookie. The cookie holds
``"<expires>:<id,id,...>.<hmac>"`` where the HMAC covers *both* the expiry and
the exact set of group ids — so a client can neither extend the lifetime nor
add a group it never unlocked. Hiding rows in the UI is cosmetic; every data
read filters server-side by :func:`unlocked_group_ids`.

Unlocking is additive: a user can hold several groups open at once and sees the
union (plus public rows). Reuses ``Settings.signing_key`` with its own
domain-separation purpose. The same caveat as edit mode applies: without
``SECRET_KEY`` the key is random per process, so unlocks won't survive a
restart or span multiple workers.
"""

from __future__ import annotations

import hmac
import time
import uuid
from collections.abc import Iterable
from hashlib import sha256

from fastapi import Request

from app.config import get_settings

_PURPOSE = b"access-groups:v1"

COOKIE_NAME = "templater_groups"
COOKIE_PATH = "/templater"
TOKEN_TTL_SECONDS = 8 * 60 * 60


def _canonical_ids(group_ids: Iterable[uuid.UUID]) -> str:
    """Sorted, comma-joined hex ids — order-independent so the same unlocked set
    always signs identically regardless of insertion order."""

    return ",".join(sorted(gid.hex for gid in group_ids))


def _signature(payload: str) -> str:
    key = get_settings().signing_key
    return hmac.new(key, _PURPOSE + b"\x00" + payload.encode("utf-8"), sha256).hexdigest()


def issue_groups_token(group_ids: Iterable[uuid.UUID], now: float | None = None) -> str:
    """Token proving "the server unlocked exactly these groups until expiry"."""

    expires_at = int(now if now is not None else time.time()) + TOKEN_TTL_SECONDS
    payload = f"{expires_at}:{_canonical_ids(group_ids)}"
    return f"{payload}.{_signature(payload)}"


def _parse(token: str | None, now: float | None = None) -> set[uuid.UUID]:
    """Return the unlocked group ids in ``token`` (empty set when the token is
    missing, malformed, tampered with, or expired)."""

    if not token or "." not in token:
        return set()
    payload, _, signature = token.rpartition(".")
    if not hmac.compare_digest(_signature(payload), signature):
        return set()
    expires_str, _, ids_str = payload.partition(":")
    try:
        expires_at = int(expires_str)
    except ValueError:
        return set()
    if expires_at < (now if now is not None else time.time()):
        return set()
    out: set[uuid.UUID] = set()
    for hex_id in ids_str.split(","):
        if not hex_id:
            continue
        try:
            out.add(uuid.UUID(hex=hex_id))
        except ValueError:
            # A single bad id invalidates the whole token — it can't have been
            # issued by us, since we control the encoding.
            return set()
    return out


def unlocked_group_ids(request: Request, now: float | None = None) -> set[uuid.UUID]:
    """The set of groups the current request has unlocked (public rows are
    always visible regardless and carry no group id)."""

    return _parse(request.cookies.get(COOKIE_NAME), now=now)
