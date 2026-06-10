"""Server-issued HMAC proofs.

The template upload flow is two stateless requests: ``preview`` runs the LLM
server-side, then a separate ``create`` persists the result. Everything bridging
them rides in client-controlled hidden form fields, so ``create`` cannot trust
its own payload as evidence that the LLM actually ran.

To mark a freshly-saved template ``import_status="processed"`` honestly, the
server signs the analysed result during ``preview`` and verifies that signature
at ``create`` time. A client-crafted POST cannot produce a valid proof without
the server's signing key, so it cannot flip the flag and bypass
``_require_processed``.

The proof binds the original ``content`` *and* the LLM-produced ``llm_meta`` (the
summary/metadata the user does not edit in review), so a client holding a valid
proof still cannot swap in empty or fabricated analysis and keep ``processed``.
Placeholders are intentionally *not* bound: the review screen lets the user remap
them before saving, so they are user data by design.
"""

from __future__ import annotations

import hmac
import json
from hashlib import sha256
from typing import Any

from app.config import get_settings

# Domain-separates this proof from any future HMAC use of the same key.
_PURPOSE = b"llm-processed:v1"


def _message(content: str, llm_meta: dict[str, Any] | None) -> bytes:
    """Canonical, order-independent encoding of the signed result.

    Newlines in ``content`` are normalised to LF before signing: the upload form
    posts preview as multipart (browsers serialise entry values with CRLF), while
    the review form posts create as urlencoded (htmx submits the textarea/hidden
    value with LF). Without canonicalisation the same template would sign as
    CRLF and verify as LF, failing for any multiline body."""

    payload = json.dumps(
        {
            "content": content.replace("\r\n", "\n").replace("\r", "\n"),
            "llm_meta": llm_meta or {},
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _PURPOSE + b"\x00" + payload.encode("utf-8")


def sign_processed(content: str, llm_meta: dict[str, Any] | None = None) -> str:
    """Proof that the server LLM-analysed this ``content`` into this ``llm_meta``."""

    key = get_settings().signing_key
    return hmac.new(key, _message(content, llm_meta), sha256).hexdigest()


def verify_processed(
    content: str, llm_meta: dict[str, Any] | None, proof: str | None
) -> bool:
    """True when ``proof`` was issued by :func:`sign_processed` for this exact
    ``content`` and ``llm_meta``."""

    if not proof:
        return False
    return hmac.compare_digest(sign_processed(content, llm_meta), proof)
