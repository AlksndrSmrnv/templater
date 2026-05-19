from __future__ import annotations

import json
from types import SimpleNamespace

from app.services.template_render import render_template_html


def _tpl(content: str, placeholders: list[dict], fmt: str = "json"):
    return SimpleNamespace(format=fmt, content=content, placeholders=placeholders)


def test_render_json_wraps_string_leaf_in_placeholder_span():
    content = json.dumps({"name": "Иванов", "amount": 100})
    placeholders = [
        {"location": "/name", "mode": "literal", "value": "Иванов", "original": "Иванов"},
        {"location": "/amount", "mode": "mapped", "value": "{{sender.account.balance}}", "original": "100"},
    ]
    html = render_template_html(_tpl(content, placeholders))
    assert 'data-idx="0"' in html
    assert "Иванов" in html
    assert 'data-idx="1"' in html
    assert "sender.account.balance" not in html  # current value is the rendered span text


def test_render_falls_back_for_invalid_json():
    html = render_template_html(_tpl("not a json {", []))
    assert "not a json" in html


def test_render_xml_wraps_text_and_attrs():
    content = '<msg type="t"><from>A</from></msg>'
    placeholders = [
        {"location": "/msg/@type", "mode": "literal", "value": "t", "original": "t"},
        {"location": "/msg/from[0]/#text", "mode": "mapped", "value": "{{sender.fullName}}", "original": "A"},
    ]
    html = render_template_html(_tpl(content, placeholders, fmt="xml"))
    assert 'data-idx="0"' in html
    assert 'data-idx="1"' in html
