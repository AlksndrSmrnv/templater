"""Render tests for the «Цепочка запросов» UI — hand-built contexts, no live DB.

Mirrors ``tests/test_page_templates.py``: renders Jinja templates with plain
dicts / SimpleNamespaces via the shared ``render_template`` helper.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace

from tests.test_page_templates import render_template


def _step(name: str, body: str, mock: str = '{"transferId": "TRF-1"}') -> dict:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "method": "POST",
        "url": "https://api.example/transfer",
        "headers": [{"key": "Authorization", "value": "Bearer x", "mode": "dynamic"}],
        "format": "json",
        "body": body,
        "body_html": body,  # server renders coloured markup; raw is fine for the seed
        "changed_locations": [],
        "mock_response": mock,
    }


def _chain_panel_context(
    steps: list[dict],
    *,
    standalone: bool = False,
    dependencies: dict | None = None,
) -> dict:
    chain = SimpleNamespace(id=uuid.uuid4(), name="Цепочка перевода")
    return {
        "standalone": standalone,
        "chain": chain,
        "chain_data": {"id": str(chain.id), "name": chain.name, "steps": steps},
        "available": [
            {"id": str(uuid.uuid4()), "name": "Перевод A2A", "method": "POST",
             "project_name": "Платежи", "project_color": "#3366ff"},
        ],
        "execute_url": "/templater/send-htmx/execute",
        # The panel renders a «Зависимости сообщений» overview from this.
        "dependencies": dependencies if dependencies is not None else {},
    }


def test_chain_panel_renders_steps_and_controls() -> None:
    html = render_template(
        "partials/chain_panel.html",
        _chain_panel_context([_step("Создать перевод", '{"amount": 100}')]),
    )
    assert "chainPanel(" in html  # Alpine component wired
    assert "Добавить шаг" in html
    assert "Запустить всё" in html
    assert "Цепочка перевода" in html
    # The reference hint advertises purple references.
    assert "placeholder reference" in html
    # Picker option from the available list.
    assert "Перевод A2A" in html
    # Inline header (delete) only when not standalone.
    assert "Удалить" in html


def test_chain_panel_drops_example_and_insert_link_adds_collapse() -> None:
    html = render_template(
        "partials/chain_panel.html",
        _chain_panel_context([_step("Создать перевод", '{"amount": 100}')]),
    )
    # No editable example response and no insert-link button on the chain page.
    assert "Пример ответа" not in html
    assert "Вставить ссылку" not in html
    # Manual body textarea is gone; the body is the server-coloured markup.
    assert "Редактировать тело" not in html
    assert 'x-html="step.bodyHtml"' in html
    # Click-to-bind + collapsible steps (collapsed by default in chain_panel.js).
    assert "onFieldClick(" in html
    assert "chain-collapse-toggle" in html
    assert 'x-show="!step.collapsed"' in html
    # Colour legend in the hint.
    assert "placeholder dynamic" in html
    assert "placeholder filled" in html


def test_chain_panel_seed_json_is_escaped() -> None:
    # A step body containing a </script> must not break out of the x-data seed.
    danger = _step("XSS", '{"x": "</script><script>alert(1)</script>"}')
    html = render_template("partials/chain_panel.html", _chain_panel_context([danger]))
    assert "<script>alert(1)</script>" not in html


def test_chain_panel_standalone_hides_inline_header() -> None:
    html = render_template(
        "partials/chain_panel.html",
        _chain_panel_context([_step("a", "{}")], standalone=True),
    )
    # The standalone page renders its own header; the partial omits the inline one.
    assert 'hx-target="#filled-panel"' not in html


def test_chain_panel_shows_dependencies() -> None:
    s1 = _step("Создать перевод", '{"amount": 100}')
    s2 = _step("Подтвердить", '{"transferId": "{{ $1.transferId }}"}')
    html = render_template(
        "partials/chain_panel.html",
        _chain_panel_context([s1, s2], dependencies={1: [], 2: [1]}),
    )
    assert "Зависимости сообщений" in html
    assert "шаг 1" in html  # step 2 depends on step 1


def test_filled_tree_renders_chain_node() -> None:
    chain_id = str(uuid.uuid4())
    tree = {
        "folders": {},
        "templates": [],
        "chains": [
            {"id": chain_id, "name": "Моя цепочка", "step_count": 3,
             "group_name_snapshot": "", "group_color_snapshot": ""},
        ],
    }
    html = render_template(
        "partials/filled_tree.html",
        {"tree": tree, "count": 0, "chain_count": 1, "search": "",
         "list_limit": 200, "truncated": False},
    )
    assert "tree-chain" in html
    assert "Моя цепочка" in html
    assert f"/templater/filled-templates-htmx/chains/{chain_id}/panel" in html
    assert "Новая цепочка в корне" in html
    # Chains present → not the empty state.
    assert "Пока нет заполненных шаблонов" not in html


def test_filled_panel_has_send_and_add_to_chain() -> None:
    ft = SimpleNamespace(
        id=uuid.uuid4(), name="Заполненный", http_method_snapshot="POST",
        url_snapshot="https://api.example/x", headers_snapshot=[], format="json",
        filled_content='{"a": 1}', changed_locations=[], unresolved=[],
        project_name_snapshot="", project_color_snapshot="",
        group_name_snapshot="", group_color_snapshot="",
        message_template_id=None, template_name_snapshot="Шаблон",
        created_at=datetime(2026, 6, 25, 12, 0),
    )
    chain_name = "Целевая цепочка"
    html = render_template(
        "partials/filled_panel.html",
        {
            "standalone": False,
            "ft": ft,
            "rendered_html": "<span>body</span>",
            "role_rows": [],
            "role_client_ids": {"sender": None, "receiver": None, "accountOwner": None},
            "alive_client_ids": set(),
            "chains": [{"id": str(uuid.uuid4()), "name": chain_name}],
        },
    )
    assert "sendOnce()" in html  # one-off mock send wired
    assert "Отправить" in html
    assert "В цепочку" in html
    assert chain_name in html  # existing chain offered in the dropdown


def test_filled_panel_chain_dropdown_empty_hint() -> None:
    ft = SimpleNamespace(
        id=uuid.uuid4(), name="Заполненный", http_method_snapshot="GET",
        url_snapshot="", headers_snapshot=[], format="json",
        filled_content="{}", changed_locations=[], unresolved=[],
        project_name_snapshot="", project_color_snapshot="",
        group_name_snapshot="", group_color_snapshot="",
        message_template_id=None, template_name_snapshot="Шаблон",
        created_at=datetime(2026, 6, 25, 12, 0),
    )
    html = render_template(
        "partials/filled_panel.html",
        {
            "standalone": False, "ft": ft, "rendered_html": "x", "role_rows": [],
            "role_client_ids": {"sender": None, "receiver": None, "accountOwner": None},
            "alive_client_ids": set(), "chains": [],
        },
    )
    assert "Нет цепочек" in html
