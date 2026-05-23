from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# A safe, lowercase PostgreSQL identifier — needs no quoting and has no
# case-folding surprises between CREATE SCHEMA and search_path.
_DB_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


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

    database_url: str = "postgresql+asyncpg://template_maker:template_maker@localhost:5432/template_maker"
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

    @property
    def llm_active(self) -> bool:
        return bool(self.gigachat_base_url and self.gigachat_cert_b64 and self.gigachat_key_b64)

    @property
    def templates_dir(self) -> Path:
        return self.base_dir / "templates"

    @property
    def static_dir(self) -> Path:
        return self.base_dir / "static"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
