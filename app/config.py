from __future__ import annotations

import re
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
