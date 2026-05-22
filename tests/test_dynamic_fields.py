from __future__ import annotations

import pytest

from app.services.dynamic_fields import (
    DYNAMIC_FIELD_TOKENS,
    DYNAMIC_TOKENS,
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
