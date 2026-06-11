from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from app.services.importers import detect_and_parse
from app.services.importers.insomnia import parse_insomnia_collection
from app.services.templates import apply_dynamic_headers
from app.utils.errors import ValidationFailed

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "insomnia_sample.json"


def _load() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_parse_sample_collection_basics() -> None:
    pc = parse_insomnia_collection(_load())
    assert pc.name == "Demo Bank"
    assert pc.description == "Insomnia export of the demo bank API"
    assert pc.source == "insomnia"
    assert pc.source_format == "v4"
    assert pc.variables == [{"key": "baseUrl", "value": "https://api.bank.test"}]
    assert len(pc.requests) == 3


def test_folder_path_built_from_parent_chain() -> None:
    pc = parse_insomnia_collection(_load())
    by_name = {r.name: r for r in pc.requests}
    assert by_name["A2A Transfer"].folder_path == ["Transfers"]
    assert by_name["Legacy XML Transfer"].folder_path == ["Transfers", "Legacy"]
    assert by_name["Health Check"].folder_path == []


def test_requests_ordered_by_meta_sort_key() -> None:
    # Folder "Transfers" (-200) sorts before the root-level health check (-100);
    # inside it the request (-100) comes before the "Legacy" subfolder (-50).
    pc = parse_insomnia_collection(_load())
    assert [r.name for r in pc.requests] == [
        "A2A Transfer",
        "Legacy XML Transfer",
        "Health Check",
    ]


def test_json_body_detected_and_parsable() -> None:
    req = next(r for r in parse_insomnia_collection(_load()).requests if r.name == "A2A Transfer")
    assert req.http_method == "POST"
    assert req.fmt == "json"
    assert req.parsable is True
    assert req.url == "https://api.bank.test/transfer/a2a"
    assert "payerName" in req.content


def test_xml_body_detected() -> None:
    req = next(
        r for r in parse_insomnia_collection(_load()).requests if r.name == "Legacy XML Transfer"
    )
    assert req.fmt == "xml"
    assert req.parsable is True


def test_get_without_body_is_not_parsable() -> None:
    req = next(r for r in parse_insomnia_collection(_load()).requests if r.name == "Health Check")
    assert req.http_method == "GET"
    assert req.parsable is False
    assert req.content == ""
    assert req.headers and req.headers[0]["key"] == "Accept"


def test_headers_preserve_disabled_flag() -> None:
    req = next(r for r in parse_insomnia_collection(_load()).requests if r.name == "A2A Transfer")
    debug = next(h for h in req.headers if h["key"] == "X-Debug")
    assert debug["disabled"] is True


def test_apply_dynamic_headers_rewrites_known_tokens() -> None:
    req = next(r for r in parse_insomnia_collection(_load()).requests if r.name == "A2A Transfer")
    headers = apply_dynamic_headers(req.headers)
    by_key = {h["key"]: h for h in headers}
    assert by_key["RqUID"]["mode"] == "dynamic"
    assert by_key["RqUID"]["value"] == "{{rqUID}}"
    assert by_key["Content-Type"]["mode"] == "literal"


def test_non_text_mime_body_skipped() -> None:
    pc = parse_insomnia_collection(
        {
            "resources": [
                {
                    "_id": "req_1",
                    "_type": "request",
                    "name": "Form",
                    "method": "POST",
                    "url": "https://x",
                    "body": {
                        "mimeType": "application/x-www-form-urlencoded",
                        "text": "a=1&b=2",
                    },
                }
            ]
        }
    )
    req = pc.requests[0]
    assert req.parsable is False
    assert req.content == ""


def test_missing_mime_type_sniffed_from_content() -> None:
    pc = parse_insomnia_collection(
        {
            "resources": [
                {
                    "_id": "req_1",
                    "_type": "request",
                    "name": "NoMime",
                    "method": "POST",
                    "body": {"mimeType": "", "text": "<a>1</a>"},
                }
            ]
        }
    )
    req = pc.requests[0]
    assert req.fmt == "xml"
    assert req.parsable is True


def test_unparsable_json_body_kept_verbatim() -> None:
    pc = parse_insomnia_collection(
        {
            "resources": [
                {
                    "_id": "req_1",
                    "_type": "request",
                    "name": "Broken",
                    "method": "POST",
                    "body": {"mimeType": "application/json", "text": "{not json"},
                }
            ]
        }
    )
    req = pc.requests[0]
    assert req.fmt == "json"
    assert req.parsable is False
    assert req.content == "{not json"


def test_broken_parent_chain_does_not_crash() -> None:
    pc = parse_insomnia_collection(
        {
            "resources": [
                {
                    "_id": "fld_1",
                    "_type": "request_group",
                    "parentId": "wrk_missing",
                    "name": "Orphan Folder",
                },
                {
                    "_id": "req_1",
                    "_type": "request",
                    "parentId": "fld_1",
                    "name": "R",
                    "method": "GET",
                    "url": "https://x",
                },
                {
                    "_id": "req_2",
                    "_type": "request",
                    "parentId": "fld_ghost",
                    "name": "Lost",
                    "method": "GET",
                    "url": "https://y",
                },
            ]
        }
    )
    by_name = {r.name: r for r in pc.requests}
    assert by_name["R"].folder_path == ["Orphan Folder"]
    assert by_name["Lost"].folder_path == []


def test_cyclic_parent_chain_still_imports_requests() -> None:
    pc = parse_insomnia_collection(
        {
            "resources": [
                {"_id": "fld_a", "_type": "request_group", "parentId": "fld_b", "name": "A"},
                {"_id": "fld_b", "_type": "request_group", "parentId": "fld_a", "name": "B"},
                {
                    "_id": "req_1",
                    "_type": "request",
                    "parentId": "fld_a",
                    "name": "Trapped",
                    "method": "GET",
                    "url": "https://x",
                },
            ]
        }
    )
    assert [r.name for r in pc.requests] == ["Trapped"]


def test_invalid_top_level_raises() -> None:
    with pytest.raises(ValidationFailed):
        parse_insomnia_collection([])
    with pytest.raises(ValidationFailed):
        parse_insomnia_collection({"_type": "export"})  # no resources


def test_detect_and_parse_dispatches_by_shape() -> None:
    assert detect_and_parse(_load()).source == "insomnia"
    assert (
        detect_and_parse(
            {"info": {"name": "P", "schema": "v2.1.0"}, "item": []}
        ).source
        == "postman"
    )
    with pytest.raises(ValidationFailed):
        detect_and_parse({"nope": 1})
    with pytest.raises(ValidationFailed):
        detect_and_parse("garbage")
