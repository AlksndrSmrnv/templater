from __future__ import annotations

import pytest

from app.services.export_import import ExportImportService, _as_dict, _as_list, _safe_label


def test_as_dict_passes_through_dict():
    assert _as_dict({"ref_entity": "currency"}) == {"ref_entity": "currency"}


def test_as_dict_parses_legacy_json_string():
    # Legacy bug: options was stored double-serialized as a JSON string.
    assert _as_dict('{"ref_entity": "currency"}') == {"ref_entity": "currency"}


def test_as_dict_rejects_non_dict_json():
    assert _as_dict('"just a string"') == {}
    assert _as_dict("[1, 2, 3]") == {}


def test_as_dict_handles_garbage_and_none():
    assert _as_dict(None) == {}
    assert _as_dict("not json at all") == {}
    assert _as_dict(42) == {}


def test_as_list_only_passes_lists():
    assert _as_list([1, 2]) == [1, 2]
    assert _as_list(None) == []
    assert _as_list({"a": 1}) == []
    assert _as_list("string") == []


def test_safe_label_handles_non_dict_rows():
    assert _safe_label({"name": "X", "id": "1"}, "name", "id") == "X"
    assert _safe_label({"id": "1"}, "name", "id") == "1"
    assert _safe_label("not a dict", "name") == "<?>"
    assert _safe_label(None, "name") == "<?>"


@pytest.mark.asyncio
async def test_import_rejects_non_dict_top_level():
    """A malformed file (top-level JSON array / string) must return an error
    summary, not raise — /api/import would otherwise 500."""

    svc = ExportImportService(session=None)
    for bad in (["a", "b"], "just a string", 42):
        summary = await svc.import_package(bad, policy="skip")
        assert summary.errors, f"expected error for {bad!r}"
        assert all(v == 0 for v in summary.created.values())
        assert all(v == 0 for v in summary.updated.values())


@pytest.mark.asyncio
async def test_import_reports_wrong_shaped_sections():
    """A file where a list section is given as an object must surface an
    explicit error rather than looking like a successful empty import."""

    svc = ExportImportService(session=None)
    package = {
        "clients": {"oops": "object instead of list"},
        "templates": "a string",
        "attribute_schema": {"also": "wrong"},
        "references": ["should be an object"],
    }
    summary = await svc.import_package(package, policy="skip")
    joined = " | ".join(summary.errors)
    assert "clients" in joined
    assert "templates" in joined
    assert "attribute_schema" in joined
    assert "references" in joined
