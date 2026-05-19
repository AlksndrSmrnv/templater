from __future__ import annotations

import base64
import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger(__name__)


def _decode_to_file(b64: str, suffix: str) -> str:
    data = base64.b64decode(b64)
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def resolve_cert_files(cert_b64: str, key_b64: str) -> tuple[str, str]:
    """Decode base64-encoded PEM cert/key into temp files and return their paths."""

    cert_path = _decode_to_file(cert_b64, suffix=".pem")
    try:
        key_path = _decode_to_file(key_b64, suffix=".pem")
    except Exception:
        remove_temp_file(cert_path)
        raise
    return cert_path, key_path


def remove_temp_file(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Failed to remove temp file %s: %s", path, exc)


@contextmanager
def cert_files(cert_b64: str, key_b64: str) -> Iterator[tuple[str, str]]:
    cert_path, key_path = resolve_cert_files(cert_b64, key_b64)
    try:
        yield cert_path, key_path
    finally:
        remove_temp_file(cert_path)
        remove_temp_file(key_path)
