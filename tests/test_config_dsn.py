from __future__ import annotations

import pytest

from app.config import Settings


def test_database_dsn_password_keeps_special_characters() -> None:
    settings = Settings(database_dsn="host=h port=5432 dbname=d user=u password='p@ss w$rd'")

    assert settings.database_url.password == "p@ss w$rd"


def test_database_dsn_password_keeps_hash_character() -> None:
    settings = Settings(database_dsn="host=h port=5432 dbname=d user=u password='p#ss'")

    assert settings.database_url.password == "p#ss"


def test_database_dsn_requires_password() -> None:
    with pytest.raises(ValueError, match="отсутствуют обязательные ключи"):
        Settings(database_dsn="host=h port=5432 dbname=d user=u")


def test_database_dsn_extra_options_go_to_url_query() -> None:
    settings = Settings(
        database_dsn=(
            "host=h port=5432 dbname=d user=u password=p "
            "sslmode=require connect_timeout=3"
        )
    )

    assert settings.database_url.query == {"sslmode": "require", "connect_timeout": "3"}


def test_database_dsn_prefers_hostaddr_over_host() -> None:
    settings = Settings(
        database_dsn="host=db.local hostaddr=10.0.0.5 port=5432 dbname=d user=u password=p"
    )

    assert settings.database_url.host == "10.0.0.5"


def test_database_dsn_allows_empty_password() -> None:
    settings = Settings(database_dsn="host=h port=5432 dbname=d user=u password=")

    assert settings.database_url.password == ""
