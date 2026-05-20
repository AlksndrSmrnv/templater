from __future__ import annotations

import base64
import os

from app.llm.certs import remove_temp_file, resolve_cert_files


def test_resolve_and_remove_round_trip() -> None:
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
