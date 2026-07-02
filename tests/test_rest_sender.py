"""The REST send seam (app/services/rest_sender.py).

No live DB and no real network: the mock branch is pure, and the real branch is
exercised with a fake ``httpx.AsyncClient`` (so the client-cert / SSLContext
wiring is asserted without a TLS server). A throwaway cert + key is read from
tests/fixtures/tls (no ``cryptography`` dependency); the JKS case packs them with
``pyjks`` — the same library the seam uses to read them back.
"""

from __future__ import annotations

import base64
import os
import ssl
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.services import rest_sender

_FIXTURES = Path(__file__).parent / "fixtures" / "tls"


def _pem_to_der(pem: str) -> bytes:
    """DER bytes from a single-block PEM (strip header/footer, base64-decode)."""

    body = "".join(line for line in pem.splitlines() if "-----" not in line)
    return base64.b64decode(body)


def _self_signed() -> dict[str, object]:
    """A throwaway self-signed client cert + PKCS#8 key (PEM + DER forms).

    Read from tests/fixtures/tls (generated once with openssl) so the suite needs
    no ``cryptography`` dependency — DER is derived by decoding the PEM body.
    """

    cert_pem = (_FIXTURES / "client_cert.pem").read_text()
    key_pem = (_FIXTURES / "client_key.pem").read_text()
    return {
        "cert_pem": cert_pem,
        "key_pem": key_pem,
        "cert_der": _pem_to_der(cert_pem),
        "key_der": _pem_to_der(key_pem),
    }


class _FakeResponse:
    def __init__(self, status_code: int, text: str, headers: dict[str, str]) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers
        self.reason_phrase = "OK"


class _FakeAsyncClient:
    """Captures constructor kwargs + the request, returns a canned response."""

    captured: dict[str, Any] = {}

    def __init__(self, **kwargs: object) -> None:
        _FakeAsyncClient.captured = dict(kwargs)

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def request(self, method, url, headers=None, content=None):  # type: ignore[no-untyped-def]
        _FakeAsyncClient.captured["request"] = {
            "method": method,
            "url": url,
            "headers": headers,
            "content": content,
        }
        return _FakeResponse(200, '{"statusCode": 0, "status": "SUCCESS"}', {"Content-Type": "application/json"})


async def _noop(latency_ms: int) -> None:
    return None


# ---------- mock branch ----------


async def test_send_mock_echoes_and_marks_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rest_sender, "_simulate_latency", _noop)
    result = await rest_sender.send_request(
        method="POST", url="https://x", headers=[], body="{}",
        mock_response='{"statusCode": 0}', tls=None,
    )
    assert result.http_status == 200
    assert result.status_text == "OK"
    assert result.ok is True
    assert result.status_code == 0
    assert result.response_body == '{"statusCode": 0}'  # echoed verbatim


async def test_send_mock_nonzero_status_code_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rest_sender, "_simulate_latency", _noop)
    result = await rest_sender.send_request(
        method="POST", url="https://x", headers=[], body="",
        mock_response='{"statusCode": 7}', tls=None,
    )
    assert result.ok is False
    assert result.status_code == 7
    assert result.error_message == ""  # mock has no transport error


# ---------- payload parsing ----------


def test_tls_from_payload_variants() -> None:
    assert rest_sender.tls_from_payload({}) is None
    assert rest_sender.tls_from_payload({"tls": "nope"}) is None
    # PEM needs both cert and key.
    assert rest_sender.tls_from_payload({"tls": {"kind": "pem", "cert": "c"}}) is None
    pem = rest_sender.tls_from_payload(
        {"tls": {"kind": "pem", "cert": "c", "key": "k", "password": "p", "verify": True}}
    )
    assert pem is not None and pem.kind == "pem" and pem.password == "p" and pem.verify is True
    jks = rest_sender.tls_from_payload({"tls": {"kind": "jks", "jks": "YWJj"}})
    assert jks is not None and jks.kind == "jks" and jks.verify is False
    assert rest_sender.tls_from_payload({"tls": {"kind": "bogus"}}) is None


# ---------- real branch (fake httpx) ----------


async def test_send_real_pem_wires_cert_and_maps_response(monkeypatch: pytest.MonkeyPatch) -> None:
    certs = _self_signed()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    tls = rest_sender.TlsMaterial(
        kind="pem", cert_pem=str(certs["cert_pem"]), key_pem=str(certs["key_pem"]), verify=False
    )
    result = await rest_sender.send_request(
        method="post", url="https://api.example",
        headers=[{"key": "X-A", "value": "1"}, {"key": "X-B", "value": "2", "disabled": True}],
        body='{"a":1}', mock_response="ignored", tls=tls,
    )
    assert result.ok is True
    assert result.http_status == 200
    assert result.status_code == 0
    # Client cert supplied via an SSLContext, verification disabled per the toggle.
    ctx = _FakeAsyncClient.captured["verify"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_NONE
    req = _FakeAsyncClient.captured["request"]
    assert req["method"] == "POST"  # normalized upper-case
    assert ("X-A", "1") in req["headers"]  # enabled header forwarded
    assert all(k != "X-B" for k, _ in req["headers"])  # disabled header dropped
    assert req["content"] == b'{"a":1}'


async def test_send_real_verify_toggle_on_keeps_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    certs = _self_signed()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    tls = rest_sender.TlsMaterial(
        kind="pem", cert_pem=str(certs["cert_pem"]), key_pem=str(certs["key_pem"]), verify=True
    )
    await rest_sender.send_request(
        method="GET", url="https://x", headers=[], body="", mock_response="", tls=tls
    )
    ctx = _FakeAsyncClient.captured["verify"]
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


async def test_send_real_jks_extracts_and_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    import jks

    certs = _self_signed()
    entry = jks.PrivateKeyEntry.new("mykey", [certs["cert_der"]], certs["key_der"], "pkcs8")
    store = jks.KeyStore.new("jks", [entry])
    blob = store.saves("secret")
    tls = rest_sender.TlsMaterial(
        kind="jks", jks_b64=base64.b64encode(blob).decode(), password="secret", verify=False
    )
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    result = await rest_sender.send_request(
        method="GET", url="https://x", headers=[], body="", mock_response="", tls=tls
    )
    assert result.ok is True
    assert result.http_status == 200


class _FakeRedirectClient(_FakeAsyncClient):
    async def request(self, method, url, headers=None, content=None):  # type: ignore[no-untyped-def]
        _FakeAsyncClient.captured["request"] = {"method": method, "url": url}
        return _FakeResponse(302, "", {"Location": "https://elsewhere"})


async def test_send_real_3xx_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # Redirects aren't followed, so a 3xx is an unhandled response — not-ok even
    # though the transport itself succeeded. Only 2xx counts as success.
    certs = _self_signed()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeRedirectClient)
    tls = rest_sender.TlsMaterial(
        kind="pem", cert_pem=str(certs["cert_pem"]), key_pem=str(certs["key_pem"]), verify=False
    )
    result = await rest_sender.send_request(
        method="GET", url="https://x", headers=[], body="", mock_response="", tls=tls
    )
    assert result.http_status == 302
    assert result.ok is False


def test_header_pairs_preserves_falsy_value_and_filters_rows() -> None:
    pairs = rest_sender._header_pairs(
        [
            {"key": "X-Zero", "value": "0"},   # falsy string must survive verbatim
            {"key": "X-None", "value": None},  # None → empty string, not dropped
            {"key": "  ", "value": "v"},       # blank key → dropped
            {"key": "X-Off", "value": "v", "disabled": True},  # disabled → dropped
        ]
    )
    assert ("X-Zero", "0") in pairs
    assert ("X-None", "") in pairs
    assert all(k.strip() for k, _ in pairs)
    assert all(k != "X-Off" for k, _ in pairs)


async def test_send_real_missing_pyjks_surfaces_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A missing pyjks must become an actionable error, not a generic failed send.
    import builtins

    real_import = builtins.__import__

    def _no_jks(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "jks":
            raise ImportError("No module named 'jks'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_jks)
    tls = rest_sender.TlsMaterial(kind="jks", jks_b64="YWJj", password="p")
    result = await rest_sender.send_request(
        method="GET", url="https://x", headers=[], body="", mock_response="", tls=tls
    )
    assert result.ok is False
    assert "pyjks" in result.error_message


async def test_send_real_reports_error_on_bad_material() -> None:
    # Garbage cert/key → OpenSSL raises inside the seam; a failed send, not a crash.
    tls = rest_sender.TlsMaterial(kind="pem", cert_pem="not a cert", key_pem="not a key")
    result = await rest_sender.send_request(
        method="GET", url="https://x", headers=[], body="", mock_response="", tls=tls
    )
    assert result.ok is False
    assert result.http_status is None
    assert result.error_message != ""


# ---------- temp-file hygiene ----------


def test_ssl_context_deletes_temp_files(monkeypatch: pytest.MonkeyPatch) -> None:
    certs = _self_signed()
    tls = rest_sender.TlsMaterial(
        kind="pem", cert_pem=str(certs["cert_pem"]), key_pem=str(certs["key_pem"]), verify=True
    )
    written: list[str] = []
    original = rest_sender._write_secret

    def _spy(pem: str, tmp_paths: list[str]) -> str:
        path = original(pem, tmp_paths)
        written.append(path)
        return path

    monkeypatch.setattr(rest_sender, "_write_secret", _spy)
    with rest_sender._client_ssl_context(tls) as ctx:
        assert isinstance(ctx, ssl.SSLContext)
        assert written and all(os.path.exists(p) for p in written)  # live during use
    assert not any(os.path.exists(p) for p in written)  # cleaned on exit
