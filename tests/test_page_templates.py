from __future__ import annotations

from app.routes.deps import get_templates


def render_template(name: str, context: dict[str, object]) -> str:
    return get_templates().env.get_template(name).render(context)


def page_context_line(html: str) -> str:
    return next(line.strip() for line in html.splitlines() if line.strip().startswith("window.PAGE"))


def test_entity_form_serializes_page_context_as_json_literals() -> None:
    html = render_template(
        "entities/form.html",
        {
            "active": "data",
            "entity_type": "client",
            "title": "Клиент",
            "entity_id": "3db678b1-1111-2222-3333-444444444444",
        },
    )

    page_line = page_context_line(html)

    assert page_line == 'window.PAGE = { entityType: "client", entityId: "3db678b1-1111-2222-3333-444444444444" };'
    assert "&#34;" not in page_line


def test_entity_form_serializes_missing_id_as_null() -> None:
    html = render_template(
        "entities/form.html",
        {
            "active": "data",
            "entity_type": "client",
            "title": "Новый клиент",
            "entity_id": None,
        },
    )

    assert page_context_line(html) == 'window.PAGE = { entityType: "client", entityId: null };'


def test_reference_form_serializes_page_context_as_json_literals() -> None:
    html = render_template(
        "references/form.html",
        {
            "active": "references",
            "entity_type": "currency",
            "title": "Валюта",
            "value_id": "5f9a67c4-1111-2222-3333-444444444444",
        },
    )

    page_line = page_context_line(html)

    assert page_line == 'window.PAGE = { entityType: "currency", valueId: "5f9a67c4-1111-2222-3333-444444444444" };'
    assert "&#34;" not in page_line


def test_list_pages_serialize_string_context_as_json_literals() -> None:
    entity_html = render_template(
        "entities/list.html",
        {
            "active": "data",
            "entity_type": "client",
            "title": 'Clients "VIP"',
        },
    )
    reference_html = render_template(
        "references/list.html",
        {
            "active": "references",
            "entity_type": "currency",
            "title": "Currencies",
        },
    )

    entity_page_line = page_context_line(entity_html)
    reference_page_line = page_context_line(reference_html)

    assert entity_page_line == 'window.PAGE = { entityType: "client", title: "Clients \\"VIP\\"" };'
    assert reference_page_line == 'window.PAGE = { entityType: "currency", isReference: true };'
    assert "&#34;" not in entity_page_line
    assert "&#34;" not in reference_page_line
