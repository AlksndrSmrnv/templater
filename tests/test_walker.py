from __future__ import annotations

import json

from app.utils import walker


def test_walk_json_collects_leaves() -> None:
    content = json.dumps(
        {
            "fullName": "Иванов",
            "passport": {"series": "4510", "number": "123456"},
            "tags": ["a", "b"],
        }
    )
    leaves = walker.walk_json(content)
    locations = {leaf.location: leaf.value for leaf in leaves}
    assert locations["/fullName"] == "Иванов"
    assert locations["/passport/series"] == "4510"
    assert locations["/passport/number"] == "123456"
    assert locations["/tags/0"] == "a"
    assert locations["/tags/1"] == "b"


def test_replace_json_substitutes_values() -> None:
    content = json.dumps({"fullName": "X", "amount": 100})
    new = walker.replace_json(
        content,
        {"/fullName": "{{sender.fullName}}", "/amount": "{{sender.account.balance}}"},
    )
    data = json.loads(new)
    assert data["fullName"] == "{{sender.fullName}}"
    assert data["amount"] == "{{sender.account.balance}}"


def test_walk_xml_collects_text_and_attrs() -> None:
    content = """<msg type="payment"><from>Иванов</from><to>Петров</to></msg>"""
    leaves = walker.walk_xml(content)
    locations = {leaf.location: leaf.value for leaf in leaves}
    assert locations["/msg/@type"] == "payment"
    assert locations["/msg/from[0]/#text"] == "Иванов"
    assert locations["/msg/to[0]/#text"] == "Петров"


def test_replace_xml_substitutes_text_and_attrs() -> None:
    content = """<msg type="payment"><from>A</from><to>B</to></msg>"""
    out = walker.replace_xml(
        content,
        {"/msg/@type": "transfer", "/msg/from[0]/#text": "{{sender.fullName}}"},
    )
    assert 'type="transfer"' in out
    assert "{{sender.fullName}}" in out
