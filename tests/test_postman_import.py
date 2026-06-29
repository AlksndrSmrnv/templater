from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from app.services.importers.postman import parse_postman_collection
from app.services.templates import apply_dynamic_headers
from app.utils.errors import ValidationFailed

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "postman_sample.json"


def _load() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_parse_sample_collection_basics() -> None:
    pc = parse_postman_collection(_load())
    assert pc.name == "Demo Bank"
    assert pc.source == "postman"
    assert pc.source_format == "v2.1.0"
    assert pc.variables == [{"key": "baseUrl", "value": "https://api.bank.test"}]
    assert len(pc.requests) == 3


def test_folder_path_is_materialised() -> None:
    pc = parse_postman_collection(_load())
    by_name = {r.name: r for r in pc.requests}
    assert by_name["A2A Transfer"].folder_path == ["Transfers"]
    assert by_name["Legacy XML Transfer"].folder_path == ["Transfers"]
    assert by_name["Health Check"].folder_path == []


def test_json_body_detected_and_parsable() -> None:
    req = next(r for r in parse_postman_collection(_load()).requests if r.name == "A2A Transfer")
    assert req.http_method == "POST"
    assert req.fmt == "json"
    assert req.parsable is True
    assert req.url == "https://api.bank.test/transfer/a2a"
    assert "payerName" in req.content


def test_xml_body_detected() -> None:
    req = next(r for r in parse_postman_collection(_load()).requests if r.name == "Legacy XML Transfer")
    assert req.fmt == "xml"
    assert req.parsable is True
    # bare-string url form
    assert req.url == "https://api.bank.test/transfer/legacy"


def test_get_without_body_is_not_parsable() -> None:
    req = next(r for r in parse_postman_collection(_load()).requests if r.name == "Health Check")
    assert req.http_method == "GET"
    assert req.parsable is False
    assert req.content == ""
    assert req.headers and req.headers[0]["key"] == "Accept"


def test_headers_preserve_disabled_flag() -> None:
    req = next(r for r in parse_postman_collection(_load()).requests if r.name == "A2A Transfer")
    debug = next(h for h in req.headers if h["key"] == "X-Debug")
    assert debug["disabled"] is True


def test_apply_dynamic_headers_rewrites_known_tokens() -> None:
    req = next(r for r in parse_postman_collection(_load()).requests if r.name == "A2A Transfer")
    headers = apply_dynamic_headers(req.headers)
    by_key = {h["key"]: h for h in headers}
    assert by_key["RqUID"]["mode"] == "dynamic"
    assert by_key["RqUID"]["value"] == "{{rqUID}}"
    assert by_key["RqUID"]["original"] == "00000000-0000-0000-0000-000000000000"
    assert by_key["Content-Type"]["mode"] == "literal"
    assert by_key["Content-Type"]["value"] == "application/json"


def test_apply_dynamic_headers_is_idempotent() -> None:
    once = apply_dynamic_headers([{"key": "RqUID", "value": "x"}])
    twice = apply_dynamic_headers(once)
    assert once == twice


def test_string_header_block_parsed() -> None:
    pc = parse_postman_collection(
        {
            "info": {"name": "S", "schema": "v2.1.0"},
            "item": [
                {
                    "name": "R",
                    "request": {
                        "method": "POST",
                        "header": "RqUID: a\nContent-Type: application/json",
                        "body": {"mode": "raw", "raw": "{}"},
                    },
                }
            ],
        }
    )
    keys = [h["key"] for h in pc.requests[0].headers]
    assert keys == ["RqUID", "Content-Type"]


def test_node_with_both_request_and_children_keeps_its_own_request() -> None:
    # A node may act as a folder (nested ``item``) *and* carry its own ``request``.
    # The parent's request must not be dropped when descending into children.
    pc = parse_postman_collection(
        {
            "info": {"name": "S", "schema": "v2.1.0"},
            "item": [
                {
                    "name": "Parent",
                    "request": {"method": "GET", "url": "https://api.bank.test/parent"},
                    "item": [
                        {
                            "name": "Child",
                            "request": {"method": "POST", "url": "https://api.bank.test/child"},
                        }
                    ],
                }
            ],
        }
    )
    by_name = {r.name: r for r in pc.requests}
    assert set(by_name) == {"Parent", "Child"}
    assert by_name["Parent"].url == "https://api.bank.test/parent"
    assert by_name["Parent"].folder_path == []
    assert by_name["Child"].url == "https://api.bank.test/child"
    assert by_name["Child"].folder_path == ["Parent"]


def test_invalid_top_level_raises() -> None:
    with pytest.raises(ValidationFailed):
        parse_postman_collection([])
    with pytest.raises(ValidationFailed):
        parse_postman_collection({"info": {"name": "x"}})  # no item
    with pytest.raises(ValidationFailed):
        parse_postman_collection({"item": []})  # no info


def test_empty_mode_raw_body_is_parsed() -> None:
    # Not-quite-Postman exports may leave ``body.mode`` empty while the payload
    # still lives in ``raw``. The body must still be imported and parsed.
    pc = parse_postman_collection(
        {
            "info": {"name": "S", "schema": "v2.1.0"},
            "item": [
                {
                    "name": "EmptyMode",
                    "request": {
                        "method": "POST",
                        "header": [],
                        "body": {"mode": "", "raw": '{"a": 1}'},
                    },
                }
            ],
        }
    )
    req = pc.requests[0]
    assert req.fmt == "json"
    assert req.parsable is True
    assert req.content == '{"a": 1}'


def test_missing_mode_raw_body_is_parsed() -> None:
    # ``body.mode`` may be omitted entirely while the payload lives in ``raw``.
    pc = parse_postman_collection(
        {
            "info": {"name": "S", "schema": "v2.1.0"},
            "item": [
                {
                    "name": "MissingMode",
                    "request": {
                        "method": "POST",
                        "body": {"raw": '{"a": 1}'},
                    },
                }
            ],
        }
    )
    req = pc.requests[0]
    assert req.fmt == "json"
    assert req.parsable is True
    assert req.content == '{"a": 1}'


def test_explicit_non_raw_mode_is_skipped() -> None:
    # A stale ``raw`` alongside an explicit non-raw mode must not be imported.
    pc = parse_postman_collection(
        {
            "info": {"name": "S", "schema": "v2.1.0"},
            "item": [
                {
                    "name": "Urlencoded",
                    "request": {
                        "method": "POST",
                        "body": {"mode": "urlencoded", "raw": '{"a": 1}'},
                    },
                }
            ],
        }
    )
    req = pc.requests[0]
    assert req.parsable is False
    assert req.content == ""


def test_unparsable_raw_body_kept_verbatim() -> None:
    pc = parse_postman_collection(
        {
            "info": {"name": "S", "schema": "v2.1.0"},
            "item": [
                {
                    "name": "Broken",
                    "request": {
                        "method": "POST",
                        "body": {"mode": "raw", "raw": "{not json", "options": {"raw": {"language": "json"}}},
                    },
                }
            ],
        }
    )
    req = pc.requests[0]
    assert req.fmt == "json"
    assert req.parsable is False
    assert req.content == "{not json"
