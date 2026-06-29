"""Server-side statusCode extraction — parity with app/static/js/status_code.js.

The history persists the same ``statusCode`` the user saw next to the send
button, so these rules must match the JS: first ``statusCode`` field
(case-insensitive) at any depth, current object before descending, numbers and
numeric strings only (booleans NOT coerced).
"""

from __future__ import annotations

from app.utils.status_code import extract_status_code


def test_top_level_number() -> None:
    assert extract_status_code('{"statusCode": 0}') == 0
    assert extract_status_code('{"statusCode": 5}') == 5


def test_case_insensitive_key() -> None:
    assert extract_status_code('{"StatusCode": 7}') == 7
    assert extract_status_code('{"STATUSCODE": 9}') == 9


def test_numeric_string_coerced() -> None:
    assert extract_status_code('{"statusCode": "12"}') == 12
    assert extract_status_code('{"statusCode": "-3"}') == -3


def test_boolean_not_coerced() -> None:
    # true/false must NOT become 1/0 — they're not a real status code.
    assert extract_status_code('{"statusCode": true}') is None


def test_nested_and_arrays() -> None:
    assert extract_status_code('{"a": {"b": {"statusCode": 4}}}') == 4
    assert extract_status_code('{"items": [{"statusCode": 8}]}') == 8


def test_current_object_before_descending() -> None:
    # The outer statusCode wins over a nested one.
    assert extract_status_code('{"statusCode": 1, "inner": {"statusCode": 2}}') == 1


def test_absent_or_invalid() -> None:
    assert extract_status_code('{"status": "SUCCESS"}') is None
    assert extract_status_code("not json") is None
    assert extract_status_code('{"statusCode": "abc"}') is None
