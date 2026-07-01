"""REST sending seam — the ONE place that turns a prepared request into a result.

Two strategies sit behind a single :func:`send_request` contract:

* **mock** (``tls is None``) — no network call: echoes the caller's editable
  ``mock_response`` after a small simulated latency, exactly as the old stub did.
  Used when a template has no configured preset connection.
* **real** (``tls`` given) — an actual HTTPS request via ``httpx`` using the
  browser-provided client certificate (PEM cert+key, or a JKS keystore). Used
  when the template's preset has a connection configured. The certificate lives
  only in the browser (sessionStorage) and arrives on this one call.

The client-cert material is held only in memory + short-lived ``0600`` temp files
that are deleted before returning; it is never persisted or logged. Keep this
module the single seam: swapping the real branch for an external "send for us"
API later means editing only :func:`_send_real`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import random
import ssl
import tempfile
import textwrap
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from app.utils.status_code import extract_status_code

# The mock's response headers/status — kept identical to the old stub so the
# history rows and UI badges are unchanged when no connection is configured.
_MOCK_RESPONSE_HEADERS = {"Content-Type": "application/json; charset=utf-8", "X-Mock-Send": "true"}
_REQUEST_TIMEOUT = 30.0


@dataclass
class TlsMaterial:
    """Client-certificate material for one real send, provided by the browser.

    Either PEM (``cert_pem`` + ``key_pem``, key optionally encrypted with
    ``password``) or a JKS keystore (``jks_b64`` + ``password``). ``verify``
    mirrors the per-preset "проверять сертификат сервера" toggle (default off so
    self-signed test endpoints work).
    """

    kind: str  # "pem" | "jks"
    cert_pem: str = ""
    key_pem: str = ""
    jks_b64: str = ""
    password: str = ""
    verify: bool = False


@dataclass
class SendResult:
    """Outcome of a send, shaped for both the JSON response and history record."""

    ok: bool
    http_status: int | None
    status_code: int | None
    response_headers: dict[str, str]
    response_body: str
    latency_ms: int | None
    status_text: str = "OK"
    error_message: str = ""


def tls_from_payload(payload: dict[str, Any]) -> TlsMaterial | None:
    """Build :class:`TlsMaterial` from a send payload's optional ``tls`` block.

    Returns ``None`` (→ mock send) when no usable connection is present: no
    ``tls`` object, an unknown ``kind``, or missing required material. The
    browser only sends a complete block, so ``None`` reliably means "no
    configured preset connection".
    """

    raw = payload.get("tls")
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    verify = bool(raw.get("verify"))
    password = str(raw.get("password") or "")
    if kind == "pem":
        cert_pem = str(raw.get("cert") or "")
        key_pem = str(raw.get("key") or "")
        if not cert_pem or not key_pem:
            return None
        return TlsMaterial(
            kind="pem", cert_pem=cert_pem, key_pem=key_pem, password=password, verify=verify
        )
    if kind == "jks":
        jks_b64 = str(raw.get("jks") or "")
        if not jks_b64:
            return None
        return TlsMaterial(kind="jks", jks_b64=jks_b64, password=password, verify=verify)
    return None


async def send_request(
    *,
    method: str,
    url: str,
    headers: Any,
    body: str,
    mock_response: str,
    tls: TlsMaterial | None,
) -> SendResult:
    """Send one request. ``tls is None`` → mock; otherwise a real mTLS send."""

    if tls is None:
        return await _send_mock(mock_response)
    return await _send_real(method=method, url=url, headers=headers, body=body, tls=tls)


# ---------- mock strategy ----------


async def _simulate_latency(latency_ms: int) -> None:
    """Mock network latency — module-local so tests can patch it."""

    await asyncio.sleep(latency_ms / 1000)


async def _send_mock(mock_response: str) -> SendResult:
    latency_ms = random.randint(35, 220)
    await _simulate_latency(latency_ms)
    status_code = extract_status_code(mock_response)
    # Transport never fails for the mock; a non-zero business statusCode in the
    # body is the only failure signal (shown red in the UI), absent/zero = ok.
    ok = status_code is None or status_code == 0
    return SendResult(
        ok=ok,
        http_status=200,
        status_code=status_code,
        response_headers=dict(_MOCK_RESPONSE_HEADERS),
        response_body=mock_response,
        latency_ms=latency_ms,
    )


# ---------- real strategy (httpx + client cert) ----------


async def _send_real(*, method: str, url: str, headers: Any, body: str, tls: TlsMaterial) -> SendResult:
    started = time.perf_counter()
    try:
        with _client_ssl_context(tls) as ctx:
            async with httpx.AsyncClient(verify=ctx, timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.request(
                    (method or "GET").upper(),
                    url,
                    headers=_header_pairs(headers),
                    content=body.encode("utf-8") if body else None,
                )
        latency_ms = int((time.perf_counter() - started) * 1000)
        response_body = resp.text
        status_code = extract_status_code(response_body)
        # Overall ok = transport succeeded (2xx/3xx) AND no non-zero business
        # statusCode in the body — matching the mock's business-code semantics.
        ok = 200 <= resp.status_code < 400 and (status_code is None or status_code == 0)
        return SendResult(
            ok=ok,
            http_status=resp.status_code,
            status_code=status_code,
            response_headers=dict(resp.headers),
            response_body=response_body,
            latency_ms=latency_ms,
            status_text=resp.reason_phrase or "",
        )
    except Exception as exc:  # noqa: BLE001 — network / TLS / cert-parse all map to a failed send
        latency_ms = int((time.perf_counter() - started) * 1000)
        return SendResult(
            ok=False,
            http_status=None,
            status_code=None,
            response_headers={},
            response_body="",
            latency_ms=latency_ms,
            status_text="",
            error_message=_error_text(exc),
        )


def _header_pairs(headers: Any) -> list[tuple[str, str]]:
    """Flatten the resolved header list ({key,value,disabled,…}) to (k, v) pairs,
    skipping disabled/blank rows. Never includes any TLS material."""

    out: list[tuple[str, str]] = []
    if isinstance(headers, list):
        for h in headers:
            if not isinstance(h, dict) or h.get("disabled"):
                continue
            key = str(h.get("key") or "").strip()
            if key:
                out.append((key, str(h.get("value") or "")))
    return out


@contextlib.contextmanager
def _client_ssl_context(tls: TlsMaterial) -> Iterator[ssl.SSLContext]:
    """Yield an SSLContext carrying the client cert; delete temp files on exit."""

    cert_pem, key_pem, key_password = _materialize_pem(tls)
    tmp_paths: list[str] = []
    try:
        cert_file = _write_secret(cert_pem, tmp_paths)
        key_file = _write_secret(key_pem, tmp_paths)
        ctx = ssl.create_default_context()
        if not tls.verify:
            # Order matters: disable hostname check before dropping verify_mode,
            # else create_default_context() raises.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file, password=key_password)
        yield ctx
    finally:
        for path in tmp_paths:
            with contextlib.suppress(OSError):
                os.remove(path)


def _materialize_pem(tls: TlsMaterial) -> tuple[str, str, str | None]:
    """Return (cert_pem, key_pem, key_password). JKS is decoded to PEM here; its
    extracted key is already decrypted, so no password is passed to OpenSSL."""

    if tls.kind == "jks":
        cert_pem, key_pem = _jks_to_pem(tls.jks_b64, tls.password)
        return cert_pem, key_pem, None
    return tls.cert_pem, tls.key_pem, (tls.password or None)


def _jks_to_pem(jks_b64: str, password: str) -> tuple[str, str]:
    """Extract the first private key + its cert chain from a JKS as PEM text."""

    import jks  # pyjks — imported lazily so the dep is only needed for JKS

    store = jks.KeyStore.loads(base64.b64decode(jks_b64), password)
    for entry in store.private_keys.values():
        if not entry.is_decrypted():
            entry.decrypt(password)
        key_pem = _der_to_pem(entry.pkey_pkcs8, "PRIVATE KEY")
        cert_pem = "".join(_der_to_pem(cert, "CERTIFICATE") for _, cert in entry.cert_chain)
        return cert_pem, key_pem
    raise ValueError("В JKS нет приватного ключа")


def _der_to_pem(der: bytes, label: str) -> str:
    body = "\n".join(textwrap.wrap(base64.b64encode(der).decode("ascii"), 64))
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n"


def _write_secret(pem: str, tmp_paths: list[str]) -> str:
    """Write PEM text to a fresh 0600 temp file, tracked for later deletion."""

    fd, path = tempfile.mkstemp(suffix=".pem")
    tmp_paths.append(path)
    os.chmod(path, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(pem)
    return path


def _error_text(exc: Exception) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__
