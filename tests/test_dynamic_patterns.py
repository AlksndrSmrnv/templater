from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.dynamic_patterns as dp
from app.services.dynamic_fields import DYNAMIC_TOKENS
from app.services.dynamic_patterns import (
    DEFAULT_DYNAMIC_PATTERNS,
    default_dynamic_patterns,
    dynamic_pattern_fields,
    load_dynamic_patterns,
    normalize_dynamic_patterns,
    save_dynamic_patterns,
)


def test_defaults_cover_every_dynamic_token() -> None:
    # The pattern map must have exactly the four canonical envelope tokens, so a
    # send never hits a field without a generation pattern.
    assert set(DEFAULT_DYNAMIC_PATTERNS) == DYNAMIC_TOKENS
    assert {f["name"] for f in dynamic_pattern_fields()} == DYNAMIC_TOKENS


def test_default_dynamic_patterns_returns_independent_copy() -> None:
    a = default_dynamic_patterns()
    a["rqUID"] = "MUTATED"
    assert default_dynamic_patterns()["rqUID"] != "MUTATED"


def test_dynamic_pattern_fields_returns_independent_copies() -> None:
    a = dynamic_pattern_fields()
    a[0]["label"] = "MUTATED"
    a.append({"name": "fake", "label": "fake"})
    b = dynamic_pattern_fields()
    assert b[0]["label"] != "MUTATED"
    assert len(b) == len(DYNAMIC_TOKENS)


def test_normalize_overlays_known_fields_only() -> None:
    out = normalize_dynamic_patterns(
        {"rqUID": "PREFIX-{uuid}", "unknown": "x", "operUID": "{rand:6}"}
    )
    assert out["rqUID"] == "PREFIX-{uuid}"
    assert out["operUID"] == "{rand:6}"
    # Untouched fields keep their defaults; the stray key is ignored.
    assert out["rqTm"] == DEFAULT_DYNAMIC_PATTERNS["rqTm"]
    assert "unknown" not in out


@pytest.mark.parametrize("bad", [None, [], "string", 42, {"rqUID": 123}])
def test_normalize_falls_back_to_defaults_for_bad_input(bad: Any) -> None:
    assert normalize_dynamic_patterns(bad) == DEFAULT_DYNAMIC_PATTERNS


def test_normalize_blank_or_whitespace_falls_back_to_default() -> None:
    out = normalize_dynamic_patterns({"rqUID": "   ", "operUID": ""})
    assert out["rqUID"] == DEFAULT_DYNAMIC_PATTERNS["rqUID"]
    assert out["operUID"] == DEFAULT_DYNAMIC_PATTERNS["operUID"]


def test_normalize_trims_and_caps_length() -> None:
    out = normalize_dynamic_patterns({"rqUID": "  {uuid}  ", "operUID": "x" * 500})
    assert out["rqUID"] == "{uuid}"
    assert len(out["operUID"]) == 200


async def test_load_merges_saved_over_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    store = {dp.DYNAMIC_PATTERNS_KEY: {"rqUID": "REQ-{seq}"}}

    class _FakeRepo:
        def __init__(self, session: Any) -> None:
            self._store = store

        async def get(self, key: str, default: Any = None) -> Any:
            return self._store.get(key, default)

    monkeypatch.setattr(dp, "SettingsRepository", _FakeRepo)
    patterns = await load_dynamic_patterns(cast(AsyncSession, object()))
    assert patterns["rqUID"] == "REQ-{seq}"
    assert patterns["operUID"] == DEFAULT_DYNAMIC_PATTERNS["operUID"]


async def test_save_persists_normalized_map(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: dict[str, Any] = {}

    class _FakeRepo:
        def __init__(self, session: Any) -> None:
            pass

        async def set(self, key: str, value: Any) -> None:
            saved[key] = value

    monkeypatch.setattr(dp, "SettingsRepository", _FakeRepo)
    result = await save_dynamic_patterns(
        cast(AsyncSession, object()), {"rqUID": "  {uuid}  ", "junk": 1}
    )
    # Blanks/unknowns normalized before persisting; every field present.
    assert saved[dp.DYNAMIC_PATTERNS_KEY] == result
    assert result["rqUID"] == "{uuid}"
    assert set(result) == DYNAMIC_TOKENS
