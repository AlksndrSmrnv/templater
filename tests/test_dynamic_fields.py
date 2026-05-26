from __future__ import annotations

import pytest

from app.services.dynamic_fields import (
    DYNAMIC_FIELD_TOKENS,
    DYNAMIC_TOKENS,
    dynamic_token_catalog,
    resolve_dynamic_token,
)


def test_dynamic_token_exports_are_consistent() -> None:
    assert DYNAMIC_FIELD_TOKENS == {
        "rquid": "rqUID",
        "operuid": "operUID",
        "rqtm": "rqTm",
        "channeldatetime": "channelDateTime",
    }
    assert frozenset(DYNAMIC_FIELD_TOKENS.values()) == DYNAMIC_TOKENS


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("/rquid", "rqUID"),
        ("/RqUID", "rqUID"),
        ("/rqUID", "rqUID"),
        ("/operuid", "operUID"),
        ("/oper_uid", "operUID"),
        ("/rqtm", "rqTm"),
        ("/RqTm", "rqTm"),
        ("/channelDateTime", "channelDateTime"),
        ("/channeldatetime", "channelDateTime"),
        ("/channel_date_time", "channelDateTime"),
        ("/Envelope/RqTm[0]/#text", "rqTm"),
        ("/Envelope/Request[0]/@channel_date_time", "channelDateTime"),
        ("/Envelope/Meta[0]/@RqUID", "rqUID"),
        ("/Envelope/[0]/oper_uid/#text", "operUID"),
    ],
)
def test_resolve_dynamic_token_handles_json_and_xml_variants(
    location: str,
    expected: str,
) -> None:
    assert resolve_dynamic_token(location) == expected


@pytest.mark.parametrize(
    "location",
    [
        "/fullName",
        "/sender/messageId",
        "/sender/rqUIDExtra",
        "/Envelope/Request[0]/#text",
        "",
    ],
)
def test_resolve_dynamic_token_returns_none_for_regular_fields(location: str) -> None:
    assert resolve_dynamic_token(location) is None


def test_dynamic_token_catalog_covers_all_known_tokens() -> None:
    catalog = dynamic_token_catalog()
    # Stable order: matches the order users see in the picker.
    assert [entry["name"] for entry in catalog] == [
        "rqUID",
        "operUID",
        "rqTm",
        "channelDateTime",
    ]
    assert {entry["name"] for entry in catalog} == DYNAMIC_TOKENS
    for entry in catalog:
        assert entry["label"], "every catalog entry must have a human label"
        assert entry["name"] in entry["label"], "label should include the raw token name"


def test_dynamic_token_catalog_returns_independent_copies() -> None:
    # Caller mutating the returned list/dicts must not poison subsequent calls.
    a = dynamic_token_catalog()
    a[0]["label"] = "MUTATED"
    a.append({"name": "fake", "label": "fake"})
    b = dynamic_token_catalog()
    assert b[0]["label"] != "MUTATED"
    assert len(b) == 4
