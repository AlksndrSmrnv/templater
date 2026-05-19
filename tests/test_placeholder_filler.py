from __future__ import annotations

import json
from xml.etree import ElementTree as ET

from app.services.placeholders import PlaceholderFiller


def test_fill_content_replaces_tokens_in_json():
    filler = PlaceholderFiller(session=None)  # session unused for fill_content
    ctx = {
        "sender": {"fullName": "Иванов", "account": {"number": "40817-100"}},
        "receiver": {"fullName": "Петров", "card": {"number": "4276-...-0001"}},
    }
    content = (
        '{"from": "{{sender.fullName}}", "fromAccount": "{{sender.account.number}}",'
        ' "to": "{{receiver.fullName}}", "toCard": "{{receiver.card.number}}",'
        ' "note": "{{sender.unknown.path}}"}'
    )
    rendered, unresolved = filler.fill_content(content, "json", ctx)
    # Result must still parse as JSON
    parsed = json.loads(rendered)
    assert parsed["from"] == "Иванов"
    assert parsed["fromAccount"] == "40817-100"
    assert parsed["to"] == "Петров"
    assert parsed["toCard"] == "4276-...-0001"
    assert parsed["note"] == "{{sender.unknown.path}}"  # not resolved — kept verbatim
    assert unresolved == ["sender.unknown.path"]


def test_fill_content_escapes_special_chars_in_json():
    """Values with quotes / backslashes must not break the JSON envelope."""

    filler = PlaceholderFiller(session=None)
    ctx = {"sender": {"fullName": 'John "The Boss" \\Doe'}}
    content = '{"name": "{{sender.fullName}}"}'
    rendered, unresolved = filler.fill_content(content, "json", ctx)
    parsed = json.loads(rendered)
    assert parsed["name"] == 'John "The Boss" \\Doe'
    assert unresolved == []


def test_fill_content_escapes_special_chars_in_xml():
    """Values with < > & must be XML-escaped, not inserted raw."""

    filler = PlaceholderFiller(session=None)
    ctx = {"sender": {"fullName": "John & <Mary>"}}
    content = "<msg><name>{{sender.fullName}}</name></msg>"
    rendered, _ = filler.fill_content(content, "xml", ctx)
    root = ET.fromstring(rendered)
    assert root.find("name").text == "John & <Mary>"


def test_fill_content_preserves_unresolved_tokens():
    filler = PlaceholderFiller(session=None)
    rendered, unresolved = filler.fill_content('{"a": "{{nope.x}}"}', "json", {})
    parsed = json.loads(rendered)
    assert parsed["a"] == "{{nope.x}}"
    assert unresolved == ["nope.x"]
