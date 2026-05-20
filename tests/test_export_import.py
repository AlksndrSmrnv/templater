from __future__ import annotations

import pytest

from app.services.export_import import (
    ExportImportService,
    _as_dict,
    _as_list,
    _safe_label,
    _validate_attributes_field,
    _validate_bool,
    _validate_object_field,
    _validate_optional_str,
    _validate_required_str,
    _validate_tags,
)


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


def test_validate_bool_strict():
    # Real bools pass through; absent → default.
    assert _validate_bool(True, False) == (True, None)
    assert _validate_bool(None, True) == (True, None)
    # The string "false" must NOT become True (bool("false") is True).
    val, err = _validate_bool("false", False)
    assert err is not None
    val, err = _validate_bool(1, False)
    assert err is not None


def test_validate_required_str():
    assert _validate_required_str("ok", 128) == ("ok", None)
    assert _validate_required_str(None, 128)[1] is not None
    assert _validate_required_str("", 128)[1] is not None
    assert _validate_required_str("   ", 128)[1] is not None
    assert _validate_required_str(123, 128)[1] is not None
    assert _validate_required_str("x" * 200, 128)[1] is not None


def test_validate_optional_str():
    assert _validate_optional_str(None) == (None, None)  # absent
    assert _validate_optional_str("text") == ("text", None)
    assert _validate_optional_str("") == ("", None)  # empty allowed for optional
    assert _validate_optional_str(42)[1] is not None


def test_validate_object_field():
    assert _validate_object_field(None) == ({}, None)  # absent / null → {}
    assert _validate_object_field({"a": 1}) == ({"a": 1}, None)
    # non-object values must error, not silently become {}
    for bad in ("bad", [], 42, True):
        obj, err = _validate_object_field(bad)
        assert obj is None
        assert err is not None


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


def test_validate_tags_accepts_list_of_strings():
    tags, err = _validate_tags(["vip", "test"])
    assert err is None
    assert tags == ["vip", "test"]


def test_validate_tags_none_means_absent():
    tags, err = _validate_tags(None)
    assert err is None
    assert tags is None  # caller keeps the existing value


def test_validate_tags_rejects_string():
    # list("vip") would explode into ["v","i","p"] — must be rejected instead.
    tags, err = _validate_tags("vip")
    assert tags is None
    assert err is not None


def test_validate_tags_rejects_non_string_items():
    tags, err = _validate_tags(["ok", 123])
    assert tags is None
    assert err is not None


def test_validate_attributes_field_absent_is_empty():
    attrs, err = _validate_attributes_field(None)
    assert err is None
    assert attrs == {}


def test_validate_attributes_field_passes_dict():
    attrs, err = _validate_attributes_field({"fullName": "X"})
    assert err is None
    assert attrs == {"fullName": "X"}


def test_validate_attributes_field_rejects_string_and_list():
    # _as_dict would silently coerce these to {} — losing/wiping attributes.
    for bad in ("oops", [1, 2], 42):
        attrs, err = _validate_attributes_field(bad)
        assert attrs is None
        assert err is not None


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
