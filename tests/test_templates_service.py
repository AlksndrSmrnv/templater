from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services.templates import TemplateService, normalize_placeholders
from app.utils.errors import ValidationFailed


def test_normalize_placeholders_accepts_valid_entries():
    raw = [
        {"location": "/a", "mode": "mapped", "value": "{{sender.x}}", "original": "X"},
        {"location": "/b", "mode": "literal", "value": "lit"},  # original defaults to ""
    ]
    out = normalize_placeholders(raw)
    assert out[0]["location"] == "/a"
    assert out[1]["original"] == ""
    assert out[1]["mode"] == "literal"


def test_normalize_placeholders_rejects_bad_mode():
    with pytest.raises(ValidationFailed):
        normalize_placeholders([{"location": "/a", "mode": "weird", "value": "v"}])


def test_normalize_placeholders_rejects_missing_location():
    with pytest.raises(ValidationFailed):
        normalize_placeholders([{"mode": "literal", "value": "v"}])


def test_normalize_placeholders_rejects_non_list_and_non_dict():
    with pytest.raises(ValidationFailed):
        normalize_placeholders({"location": "/a"})
    with pytest.raises(ValidationFailed):
        normalize_placeholders(["not-a-dict"])


def test_normalize_placeholders_none_is_empty():
    assert normalize_placeholders(None) == []


def test_regenerate_content_uses_original_content_as_source():
    """If original_content drifts out of sync with content, regenerate uses
    the (now stale) original. This documents why update() must keep
    original_content in lockstep with content on PUT."""

    template = SimpleNamespace(
        format="json",
        content='{"a": "{{sender.name}}"}',
        original_content='{"a": "Старое"}',
        placeholders=[
            {
                "location": "/a",
                "mode": "mapped",
                "value": "{{sender.name}}",
                "original": "Старое",
            }
        ],
    )
    result = TemplateService.regenerate_content(template)
    parsed = json.loads(result)
    assert parsed["a"] == "{{sender.name}}"


def test_regenerate_content_returns_original_when_no_placeholders():
    template = SimpleNamespace(
        format="json",
        content="will-be-ignored",
        original_content='{"a": 1}',
        placeholders=[],
    )
    assert TemplateService.regenerate_content(template) == '{"a": 1}'


def test_heuristic_mappings_matches_by_tail():
    leaves = [
        type("L", (), {"location": "/fullName", "value": "Иванов"})(),
        type("L", (), {"location": "/passport/series", "value": "4510"})(),
        type("L", (), {"location": "/notes", "value": "Что-то"})(),
    ]
    catalog = [
        {"path": "sender.fullName", "label": "Sender — ФИО", "data_type": "string"},
        {"path": "sender.account.series", "label": "Sender — Серия", "data_type": "string"},
    ]
    out = TemplateService._heuristic_mappings(leaves, catalog)
    assert out["/fullName"]["suggestion"] == "sender.fullName"
    assert out["/passport/series"]["suggestion"] == "sender.account.series"
    assert "/notes" not in out
