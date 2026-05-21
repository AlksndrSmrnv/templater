from __future__ import annotations

import base64
import os

import pytest

from app.llm.certs import remove_temp_file, resolve_cert_files
from app.utils.errors import LLMUnavailable


def test_resolve_and_remove_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.llm.certs._validate_cert_chain", lambda cert_path, key_path: None)

    cert_text = b"-----BEGIN CERT-----\nFAKE\n-----END CERT-----\n"
    key_text = b"-----BEGIN KEY-----\nFAKEKEY\n-----END KEY-----\n"
    cert_b64 = base64.b64encode(cert_text).decode()
    key_b64 = base64.b64encode(key_text).decode()
    cert_path, key_path = resolve_cert_files(cert_b64, key_b64)
    try:
        assert os.path.exists(cert_path)
        assert os.path.exists(key_path)
        with open(cert_path, "rb") as f:
            assert f.read() == cert_text
        with open(key_path, "rb") as f:
            assert f.read() == key_text
    finally:
        remove_temp_file(cert_path)
        remove_temp_file(key_path)
    assert not os.path.exists(cert_path)
    assert not os.path.exists(key_path)


def test_resolve_rejects_invalid_base64() -> None:
    key_b64 = base64.b64encode(b"-----BEGIN KEY-----\nFAKE\n-----END KEY-----\n").decode()

    with pytest.raises(LLMUnavailable, match="GIGACHAT_CERT_B64.*base64"):
        resolve_cert_files("not a valid base64 value", key_b64)


def test_resolve_rejects_decoded_non_pem_bytes() -> None:
    cert_b64 = base64.b64encode(b"\x30\x82not-a-pem").decode()
    key_b64 = base64.b64encode(b"-----BEGIN KEY-----\nFAKE\n-----END KEY-----\n").decode()

    with pytest.raises(LLMUnavailable, match="GIGACHAT_CERT_B64.*PEM"):
        resolve_cert_files(cert_b64, key_b64)


def test_resolve_wraps_cert_chain_load_error() -> None:
    cert_b64 = base64.b64encode(
        b"-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n"
    ).decode()
    key_b64 = base64.b64encode(
        b"-----BEGIN RSA PRIVATE KEY-----\nFAKE\n-----END RSA PRIVATE KEY-----\n"
    ).decode()

    with pytest.raises(LLMUnavailable, match="OpenSSL.*сертификат/ключ"):
        resolve_cert_files(cert_b64, key_b64)
