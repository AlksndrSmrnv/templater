from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.routes import templates_reg
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
    assert 'hx-put="/references-htmx/currency/5f9a67c4-1111-2222-3333-444444444444"' in html
    assert "reference-form.js" not in html


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
    assert 'hx-get="/references-htmx/currency/table"' in reference_html
    assert "reference-list.js" not in reference_html


def test_import_page_uses_htmx_upload_form() -> None:
    html = render_template(
        "import.html",
        {
            "active": "data",
            "default_policy": "skip",
        },
    )

    assert 'hx-post="/import-htmx"' in html
    assert 'hx-encoding="multipart/form-data"' in html
    assert "htmx.org@2.0.4" in html
    assert "alpinejs@3.14.1" in html
    assert "/api/import" not in html


def test_entity_list_has_table_meta_and_detail_drawer() -> None:
    html = render_template(
        "entities/list.html",
        {
            "active": "data",
            "entity_type": "client",
            "title": "Клиенты",
        },
    )

    assert 'id="table-meta"' in html
    assert 'id="detail-drawer"' in html
    assert '<tr id="thead-row"></tr>' in html
    assert 'id="select-all"' not in html


def test_template_fill_page_uses_role_panels_and_account_owner_flag() -> None:
    html = render_template(
        "templates_reg/fill.html",
        {
            "active": "templates",
            "template": SimpleNamespace(
                id="3db678b1-1111-2222-3333-444444444444",
                name="Fill test",
                format="json",
            ),
            "has_account_owner": True,
        },
    )

    assert 'id="role-panels"' in html
    assert "hasAccountOwner: true" in html
    assert 'id="sender-client"' not in html


@pytest.mark.asyncio
async def test_page_fill_detects_account_owner_from_placeholders_despite_false_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template_id = uuid.uuid4()
    template = SimpleNamespace(
        id=template_id,
        name="Persisted flag",
        format="json",
        llm_meta={"has_account_owner": False},
        placeholders=[{"suggestion": "accountOwner.ownerName", "value": "{{accountOwner.ownerName}}"}],
        content='{"ownerName": "{{accountOwner.ownerName}}"}',
        original_content='{"ownerName": "Иванов"}',
    )

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get(self, requested_id: uuid.UUID) -> Any:
            assert requested_id == template_id
            return template

    class FakeTemplates:
        def TemplateResponse(self, request: object, name: str, context: dict[str, object]) -> Any:
            return SimpleNamespace(name=name, context=context)

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)

    response = cast(
        Any,
        await templates_reg.page_fill(
            template_id,
            request=cast(Any, SimpleNamespace()),
            templates=cast(Any, FakeTemplates()),
            session=cast(Any, SimpleNamespace()),
        ),
    )

    assert response.name == "templates_reg/fill.html"
    assert response.context["has_account_owner"] is True


@pytest.mark.asyncio
async def test_page_fill_detects_account_owner_from_template_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template_id = uuid.uuid4()
    template = SimpleNamespace(
        id=template_id,
        name="Structure flag",
        format="json",
        llm_meta={"has_account_owner": False},
        placeholders=[],
        content='{"root": {"accountOwner": {"client": {"fullName": "Иванов"}}}}',
        original_content='{"root": {"accountOwner": {"client": {"fullName": "Иванов"}}}}',
    )

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get(self, requested_id: uuid.UUID) -> Any:
            assert requested_id == template_id
            return template

    class FakeTemplates:
        def TemplateResponse(self, request: object, name: str, context: dict[str, object]) -> Any:
            return SimpleNamespace(name=name, context=context)

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)

    response = cast(
        Any,
        await templates_reg.page_fill(
            template_id,
            request=cast(Any, SimpleNamespace()),
            templates=cast(Any, FakeTemplates()),
            session=cast(Any, SimpleNamespace()),
        ),
    )

    assert response.name == "templates_reg/fill.html"
    assert response.context["has_account_owner"] is True


@pytest.mark.asyncio
async def test_page_fill_keeps_account_owner_false_without_meta_placeholders_or_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template_id = uuid.uuid4()
    template = SimpleNamespace(
        id=template_id,
        name="No owner",
        format="json",
        llm_meta={"has_account_owner": False},
        placeholders=[],
        content='{"sender": {"fullName": "Иванов"}}',
        original_content='{"sender": {"fullName": "Иванов"}}',
    )

    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get(self, requested_id: uuid.UUID) -> Any:
            assert requested_id == template_id
            return template

    class FakeTemplates:
        def TemplateResponse(self, request: object, name: str, context: dict[str, object]) -> Any:
            return SimpleNamespace(name=name, context=context)

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)

    response = cast(
        Any,
        await templates_reg.page_fill(
            template_id,
            request=cast(Any, SimpleNamespace()),
            templates=cast(Any, FakeTemplates()),
            session=cast(Any, SimpleNamespace()),
        ),
    )

    assert response.name == "templates_reg/fill.html"
    assert response.context["has_account_owner"] is False
