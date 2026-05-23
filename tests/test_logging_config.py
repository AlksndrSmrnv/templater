from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from app.utils.logging import configure_logging


@pytest.fixture(autouse=True)
def reset_structlog() -> Iterator[None]:
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()


def test_configure_logging_renders_stdlib_logs_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json_output=True)

    logging.getLogger("tests.logging").warning("hello %s", "world")

    line = capsys.readouterr().out.strip()
    payload = json.loads(line)

    assert payload["event"] == "hello world"
    assert payload["level"] == "warning"
    assert payload["logger"] == "tests.logging"
    assert "timestamp" in payload


def test_configure_logging_preserves_caplog_handler(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging("INFO", json_output=False)

    logging.getLogger("tests.logging").warning("captured")

    assert "captured" in caplog.text
