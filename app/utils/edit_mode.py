"""Settings edit mode: a server-issued, time-limited HMAC cookie.

The app has no user accounts by design. The settings page (LLM prompts,
attribute definitions, import policy) is read-only by default; someone who
knows the ``SETTINGS_EDIT_KEY`` phrase can unlock editing. On a successful
unlock the server sets a cookie holding ``"<expires_at>.<hmac>"`` — the HMAC
covers the expiry timestamp, so the client can neither extend the lifetime nor
mint a token without the server's signing key. Hiding the edit controls in the
UI is cosmetic; every mutating settings endpoint verifies this cookie.

Reuses ``Settings.signing_key`` (same key as the LLM-processed proofs) with its
own domain-separation purpose, so the two HMAC uses can never collide. Note the
documented ``signing_key`` caveat applies here too: without ``SECRET_KEY`` the
key is random per process, so an unlock won't survive a restart.
"""

from __future__ import annotations

import hmac
import time
from hashlib import sha256

from fastapi import Request

from app.config import get_settings

_PURPOSE = b"settings-edit:v1"

COOKIE_NAME = "templater_edit"
TOKEN_TTL_SECONDS = 8 * 60 * 60


def _signature(expires_at: int) -> str:
    key = get_settings().signing_key
    return hmac.new(key, _PURPOSE + b"\x00" + str(expires_at).encode(), sha256).hexdigest()


def issue_edit_token(now: float | None = None) -> str:
    """Token proving "the server unlocked editing until ``expires_at``"."""

    expires_at = int(now if now is not None else time.time()) + TOKEN_TTL_SECONDS
    return f"{expires_at}.{_signature(expires_at)}"


def verify_edit_token(token: str | None, now: float | None = None) -> bool:
    """True when ``token`` was issued by :func:`issue_edit_token` and has not
    expired."""

    if not token or "." not in token:
        return False
    expires_str, _, signature = token.partition(".")
    try:
        expires_at = int(expires_str)
    except ValueError:
        return False
    if expires_at < (now if now is not None else time.time()):
        return False
    return hmac.compare_digest(_signature(expires_at), signature)


def check_edit_key(candidate: str) -> bool:
    """True when ``candidate`` matches the configured ``SETTINGS_EDIT_KEY``.

    An unset key means unlocking is impossible, not that any input passes."""

    configured = get_settings().settings_edit_key
    if not configured or not candidate:
        return False
    return hmac.compare_digest(configured.encode("utf-8"), candidate.encode("utf-8"))


def is_edit_mode(request: Request) -> bool:
    return verify_edit_token(request.cookies.get(COOKIE_NAME))
