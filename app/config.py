from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
