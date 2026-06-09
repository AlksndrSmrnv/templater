"""Server-issued HMAC proofs.

The template upload flow is two stateless requests: ``preview`` runs the LLM
server-side, then a separate ``create`` persists the result. Everything bridging
them rides in client-controlled hidden form fields, so ``create`` cannot trust
its own payload as evidence that the LLM actually ran.

To mark a freshly-saved template ``import_status="processed"`` honestly, the
server signs the analysed content during ``preview`` and verifies that signature
at ``create`` time. A client-crafted POST cannot produce a valid proof without
the server's signing key, so it cannot flip the flag and bypass
``_require_processed``.
"""

from __future__ import annotations

import hmac
from hashlib import sha256

from app.config import get_settings

# Domain-separates this proof from any future HMAC use of the same key.
_PURPOSE = b"llm-processed:v1"


def sign_processed(content: str) -> str:
    """Proof that the server LLM-analysed exactly this ``content``."""

    key = get_settings().signing_key
    message = _PURPOSE + b"\x00" + content.encode("utf-8")
    return hmac.new(key, message, sha256).hexdigest()


def verify_processed(content: str, proof: str | None) -> bool:
    """True when ``proof`` was issued by :func:`sign_processed` for ``content``."""

    if not proof:
        return False
    return hmac.compare_digest(sign_processed(content), proof)
