from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ["LOG_JSON"] = "false"
os.environ["LOG_LEVEL"] = "WARNING"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def _quiet_test_logging_env() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("LOG_JSON", "false")
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        yield
