from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

from app.db.models import MessageTemplate
from app.services.template_render import (
    RenderableTemplate,
    render_chain_step_html,
    render_filled_html,
    render_template_html,
)


def _tpl(
    content: str,
    placeholders: list[dict[str, Any]],
    fmt: str = "json",
) -> RenderableTemplate:
    return cast(RenderableTemplate, SimpleNamespace(format=fmt, content=content, placeholders=placeholders))


def test_render_json_wraps_string_leaf_in_placeholder_span() -> None:
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


def test_render_falls_back_for_invalid_json() -> None:
    html = render_template_html(_tpl("not a json {", []))
    assert "not a json" in html


def test_render_xml_wraps_text_and_attrs() -> None:
    content = '<msg type="t"><from>A</from></msg>'
    placeholders = [
        {"location": "/msg/@type", "mode": "literal", "value": "t", "original": "t"},
        {"location": "/msg/from[0]/#text", "mode": "mapped", "value": "{{sender.fullName}}", "original": "A"},
    ]
    html = render_template_html(_tpl(content, placeholders, fmt="xml"))
    assert 'data-idx="0"' in html
    assert 'data-idx="1"' in html


def test_render_filled_html_marks_changed_json_leaves() -> None:
    content = json.dumps({"from": "Иванов", "note": "{{sender.unknown.path}}"}, ensure_ascii=False)
    html = render_filled_html("json", content, ["/from"])
    assert 'class="placeholder filled"' in html
    assert ">Иванов</span>" in html
    assert "{{sender.unknown.path}}" in html
    assert "{{sender.unknown.path}}</span>" not in html


def test_render_filled_html_marks_changed_xml_leaves() -> None:
    content = "<msg><from>Иванов</from><note>{{sender.unknown.path}}</note></msg>"
    html = render_filled_html("xml", content, ["/msg/from[0]/#text"])
    assert 'class="placeholder filled"' in html
    assert ">Иванов</span>" in html
    assert "{{sender.unknown.path}}" in html
    assert "{{sender.unknown.path}}</span>" not in html


def test_render_chain_step_html_colours_each_leaf_by_role() -> None:
    content = json.dumps(
        {
            "amount": "100",            # filled with test data -> green
            "rqUID": "{{rqUID}}",       # dynamic by default -> blue
            "transferId": "{{ $1.transferId }}",  # reference -> purple
            "note": "static",           # untouched literal -> white/plain
        },
        ensure_ascii=False,
    )
    html = render_chain_step_html("json", content, ["/amount"])
    # Every leaf carries its location for the click-to-bind handler.
    assert 'data-location="/amount"' in html
    assert 'data-location="/note"' in html
    # Roles map to the expected colour classes.
    assert 'class="placeholder filled" data-idx="0" data-location="/amount"' in html
    assert 'class="placeholder dynamic"' in html
    assert 'class="placeholder reference"' in html
    assert 'class="placeholder plain"' in html


def test_render_chain_step_html_reference_beats_filled() -> None:
    # A location both marked changed and now holding a reference reads purple.
    content = json.dumps({"id": "{{ $1.id }}"}, ensure_ascii=False)
    html = render_chain_step_html("json", content, ["/id"])
    assert 'class="placeholder reference"' in html
    assert 'class="placeholder filled"' not in html


def test_render_chain_step_html_falls_back_for_invalid_json() -> None:
    html = render_chain_step_html("json", "not json {", [])
    assert "placeholder" not in html
    assert "not json" in html


def test_regenerate_content_skips_stale_paths() -> None:
    """A placeholder whose location no longer exists in the document must be
    skipped, not crash the render."""

    from app.services.templates import TemplateService

    template = SimpleNamespace(
        format="json",
        content='{"a": "x"}',
        original_content='{"a": "x"}',
        placeholders=[
            {"location": "/a", "mode": "mapped", "value": "{{sender.name}}", "original": "x"},
            {"location": "/does/not/exist", "mode": "mapped", "value": "{{sender.y}}", "original": "?"},
        ],
    )
    result = TemplateService.regenerate_content(cast(MessageTemplate, template))
    parsed = json.loads(result)
    assert parsed["a"] == "{{sender.name}}"


def test_regenerate_content_skips_malformed_placeholder() -> None:
    from app.services.templates import TemplateService

    template = SimpleNamespace(
        format="json",
        content='{"a": "x"}',
        original_content='{"a": "x"}',
        placeholders=[
            {"mode": "mapped"},  # no location / value
            "not-a-dict",
            {"location": "/a", "mode": "literal", "value": "Y"},
        ],
    )
    result = TemplateService.regenerate_content(cast(MessageTemplate, template))
    assert json.loads(result)["a"] == "Y"
