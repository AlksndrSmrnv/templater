from __future__ import annotations

import base64
import binascii
import logging
import os
import ssl
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

from app.utils.errors import LLMUnavailable

log = logging.getLogger(__name__)


def _decode_to_file(b64: str, suffix: str, *, env_var: str, label: str) -> str:
    try:
        data = base64.b64decode(b64.strip(), validate=True)
    except binascii.Error as exc:
        raise LLMUnavailable(f"{env_var} — не корректный base64 ({label}).") from exc

    if b"-----BEGIN" not in data:
        raise LLMUnavailable(
            f"{env_var} — декодированные байты не похожи на PEM ({label}): "
            "нет маркера -----BEGIN. Вероятно, файл в формате DER/PKCS#12 "
            "или в переменную попал не base64 от PEM-файла; сконвертируйте его в PEM и перекодируйте."
        )

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


def _validate_cert_chain(cert_path: str, key_path: str) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    except ssl.SSLError as exc:
        raise LLMUnavailable(
            "GIGACHAT_CERT_B64/GIGACHAT_KEY_B64 — PEM читается, но OpenSSL "
            "не может загрузить пару сертификат/ключ. Проверьте, что private key "
            "не зашифрован, соответствует сертификату и оба PEM-файла не повреждены. "
            f"Исходная ошибка: {exc}"
        ) from exc
    except OSError as exc:
        raise LLMUnavailable(
            "GIGACHAT_CERT_B64/GIGACHAT_KEY_B64 — не удалось прочитать временные "
            f"PEM-файлы сертификата и ключа: {exc}"
        ) from exc


def resolve_cert_files(cert_b64: str, key_b64: str) -> tuple[str, str]:
    """Decode base64-encoded PEM cert/key into temp files and return their paths."""

    cert_path = _decode_to_file(
        cert_b64,
        suffix=".pem",
        env_var="GIGACHAT_CERT_B64",
        label="сертификат",
    )
    try:
        key_path = _decode_to_file(
            key_b64,
            suffix=".pem",
            env_var="GIGACHAT_KEY_B64",
            label="ключ",
        )
    except Exception:
        remove_temp_file(cert_path)
        raise
    try:
        _validate_cert_chain(cert_path, key_path)
    except Exception:
        remove_temp_file(cert_path)
        remove_temp_file(key_path)
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
