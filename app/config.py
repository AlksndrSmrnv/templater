from __future__ import annotations

import hashlib
import logging
import re
import secrets
import shlex
from functools import cached_property, lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

# A safe, lowercase PostgreSQL identifier — needs no quoting and has no
# case-folding surprises between CREATE SCHEMA and search_path.
_DB_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_DSN_REQUIRED_KEYS = frozenset({"host", "port", "dbname", "user", "password"})
_DSN_CORE_KEYS = frozenset({"host", "hostaddr", "port", "dbname", "user", "password"})
_DRIVERNAME = "postgresql+asyncpg"


def _parse_libpq_dsn(dsn: str) -> dict[str, str]:
    # posix=True parses shell-style quoting; comments=False keeps '#'
    # as a literal inside values (libpq does not use '#' comments here).
    tokens = shlex.split(dsn, posix=True, comments=False)
    out: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            raise ValueError(f"DATABASE_DSN: токен без '=': {token!r}")
        key, _, value = token.partition("=")
        out[key.strip().lower()] = value

    missing = _DSN_REQUIRED_KEYS - out.keys()
    if missing:
        raise ValueError(f"DATABASE_DSN: отсутствуют обязательные ключи: {sorted(missing)}")
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_debug: bool = False
    # Secret for server-side HMAC proofs (e.g. "this content was LLM-analysed
    # during preview"). Optional: when empty, a stable key is derived from the
    # DB DSN so the feature needs no extra configuration. Set it explicitly to
    # rotate or share a key across heterogeneous deployments.
    secret_key: str = ""
    # Key phrase that unlocks editing on the settings page (prompts, attributes,
    # import policy). Empty = settings stay read-only for everyone.
    settings_edit_key: str = ""
    log_level: str = "INFO"
    log_json: bool = True

    database_dsn: str = (
        "host=localhost port=5432 dbname=template_maker "
        "user=template_maker password=template_maker"
    )
    # Dedicated PostgreSQL schema the app lives in. Set as the connection
    # search_path, so every table / query is isolated to it.
    db_schema: str = "templater"

    gigachat_base_url: str = ""
    gigachat_cert_b64: str = ""
    gigachat_key_b64: str = ""
    gigachat_model: str = "GigaChat-3-Ultra"

    llm_timeout: float = 120.0
    llm_concurrency: int = 3
    llm_request_delay: float = 2.0
    llm_max_retries: int = 5
    llm_retry_base_delay: float = 3.0
    # Max LLM calls for one field-mapping run: first call + retries for leaves a
    # weak model failed to map (truncated answer, empty field, confused id/path).
    llm_field_mapping_max_attempts: int = 2

    base_dir: Path = Field(default_factory=lambda: Path(__file__).resolve().parent)

    @field_validator("db_schema")
    @classmethod
    def _check_db_schema(cls, value: str) -> str:
        # db_schema is interpolated into CREATE SCHEMA / search_path — reject
        # anything that isn't a plain identifier (e.g. "templater,public" or a
        # value with quotes) before it can reach SQL.
        if not _DB_SCHEMA_RE.match(value) or len(value) > 63:
            raise ValueError(
                "DB_SCHEMA должен быть простым идентификатором PostgreSQL: "
                "строчные латинские буквы, цифры и подчёркивание, начинается с "
                "буквы или подчёркивания, не длиннее 63 символов"
            )
        return value

    @field_validator("database_dsn")
    @classmethod
    def _check_database_dsn(cls, value: str) -> str:
        _parse_libpq_dsn(value)
        return value

    @property
    def llm_active(self) -> bool:
        return bool(self.gigachat_base_url and self.gigachat_cert_b64 and self.gigachat_key_b64)

    @property
    def templates_dir(self) -> Path:
        return self.base_dir / "templates"

    @property
    def static_dir(self) -> Path:
        return self.base_dir / "static"

    @cached_property
    def signing_key(self) -> bytes:
        """Key for HMAC proofs the server issues and later verifies (no client
        ever sees it).

        Uses ``SECRET_KEY`` when set. When unset, falls back to a *random
        per-process* key — never to anything derived from public config (the
        default DSN ships in ``docker-compose.yml``, so a DSN-derived key would
        be trivially guessable). The random fallback keeps single-process/dev
        deployments working with zero config; multi-worker deployments must set
        ``SECRET_KEY`` so all workers share one key. Without it, a preview signed
        on one worker simply won't verify on another and the panel falls back to
        the full «Обработать LLM» action — it never accepts a forgeable proof."""

        if self.secret_key:
            material = self.secret_key.encode("utf-8")
        else:
            logging.getLogger(__name__).warning(
                "SECRET_KEY is not set: using a random per-process signing key. "
                "Set SECRET_KEY so 'processed' proofs stay valid across restarts "
                "and across multiple workers."
            )
            material = secrets.token_bytes(32)
        return hashlib.sha256(b"template-maker:signing:v1:" + material).digest()

    @cached_property
    def _database_dsn_parts(self) -> dict[str, str]:
        return _parse_libpq_dsn(self.database_dsn)

    @property
    def database_url(self) -> URL:
        parts = self._database_dsn_parts
        query = {key: value for key, value in parts.items() if key not in _DSN_CORE_KEYS}
        return URL.create(
            drivername=_DRIVERNAME,
            username=parts["user"],
            password=parts["password"],
            host=parts.get("hostaddr") or parts["host"],
            port=int(parts["port"]),
            database=parts["dbname"],
            query=query,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
