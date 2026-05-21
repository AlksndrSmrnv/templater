from __future__ import annotations

import json
from typing import cast
from xml.etree import ElementTree as ET

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.placeholders import PlaceholderFiller


def _session() -> AsyncSession:
    return cast(AsyncSession, None)


def test_fill_content_replaces_tokens_in_json() -> None:
    filler = PlaceholderFiller(session=_session())  # session unused for fill_content
    ctx = {
        "sender": {"fullName": "Иванов", "account": {"number": "40817-100"}},
        "receiver": {"fullName": "Петров", "card": {"number": "4276-...-0001"}},
    }
    content = (
        '{"from": "{{sender.fullName}}", "fromAccount": "{{sender.account.number}}",'
        ' "to": "{{receiver.fullName}}", "toCard": "{{receiver.card.number}}",'
        ' "note": "{{sender.unknown.path}}"}'
    )
    rendered, unresolved, changed = filler.fill_content(content, "json", ctx)
    # Result must still parse as JSON
    parsed = json.loads(rendered)
    assert parsed["from"] == "Иванов"
    assert parsed["fromAccount"] == "40817-100"
    assert parsed["to"] == "Петров"
    assert parsed["toCard"] == "4276-...-0001"
    assert parsed["note"] == "{{sender.unknown.path}}"  # not resolved — kept verbatim
    assert unresolved == ["sender.unknown.path"]
    assert set(changed) == {"/from", "/fromAccount", "/to", "/toCard"}
    assert "/note" not in changed


def test_fill_content_escapes_special_chars_in_json() -> None:
    """Values with quotes / backslashes must not break the JSON envelope."""

    filler = PlaceholderFiller(session=_session())
    ctx = {"sender": {"fullName": 'John "The Boss" \\Doe'}}
    content = '{"name": "{{sender.fullName}}"}'
    rendered, unresolved, _ = filler.fill_content(content, "json", ctx)
    parsed = json.loads(rendered)
    assert parsed["name"] == 'John "The Boss" \\Doe'
    assert unresolved == []


def test_fill_content_escapes_special_chars_in_xml() -> None:
    """Values with < > & must be XML-escaped, not inserted raw."""

    filler = PlaceholderFiller(session=_session())
    ctx = {"sender": {"fullName": "John & <Mary>"}}
    content = "<msg><name>{{sender.fullName}}</name></msg>"
    rendered, _, _ = filler.fill_content(content, "xml", ctx)
    root = ET.fromstring(rendered)
    name = root.find("name")
    assert name is not None
    assert name.text == "John & <Mary>"


def test_fill_content_preserves_unresolved_tokens() -> None:
    filler = PlaceholderFiller(session=_session())
    rendered, unresolved, _ = filler.fill_content('{"a": "{{nope.x}}"}', "json", {})
    parsed = json.loads(rendered)
    assert parsed["a"] == "{{nope.x}}"
    assert unresolved == ["nope.x"]


def test_fill_content_refuses_unparsable_json() -> None:
    """When the template doesn't parse as declared format, we must NOT silently
    fall back to raw-text substitution — that would re-introduce the escaping
    bug. The caller gets a ValidationFailed instead."""

    import pytest

    from app.utils.errors import ValidationFailed

    filler = PlaceholderFiller(session=_session())
    with pytest.raises(ValidationFailed):
        filler.fill_content("this is not json {{sender.x}}", "json", {"sender": {"x": "v"}})


def test_fill_content_refuses_unparsable_xml() -> None:
    import pytest

    from app.utils.errors import ValidationFailed

    filler = PlaceholderFiller(session=_session())
    with pytest.raises(ValidationFailed):
        filler.fill_content("<broken {{sender.x}}", "xml", {"sender": {"x": "v"}})
