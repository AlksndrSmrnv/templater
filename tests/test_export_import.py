from __future__ import annotations

from app.services.export_import import _as_dict


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
