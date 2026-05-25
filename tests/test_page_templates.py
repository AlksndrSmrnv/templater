from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.routes import templates_reg
from app.routes.deps import get_templates


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def render_template(name: str, context: dict[str, object]) -> str:
    return get_templates().env.get_template(name).render(context)


def install_page_fill_fakes(monkeypatch: pytest.MonkeyPatch, template: Any) -> None:
    class FakeTemplateService:
        def __init__(self, session: object) -> None:
            self.session = session

        async def get(self, requested_id: uuid.UUID) -> Any:
            assert requested_id == template.id
            return template

    async def fake_fill_labels(session: object) -> dict[str, dict[str, str]]:
        return {"client": {}, "account": {}, "card": {}}

    async def fake_list_all(self: object) -> list[object]:
        return []

    monkeypatch.setattr(templates_reg, "TemplateService", FakeTemplateService)
    monkeypatch.setattr(templates_reg.ClientService, "list_all", fake_list_all)
    monkeypatch.setattr(templates_reg, "_fill_labels", fake_fill_labels)


def test_entity_form_uses_htmx_update_without_static_js() -> None:
    entity = SimpleNamespace(tags=['vip "quoted"'], attributes={})
    html = render_template(
        "entities/form.html",
        {
            "active": "data",
            "entity_type": "client",
            "title": "Клиент",
            "entity_id": "3db678b1-1111-2222-3333-444444444444",
            "entity": entity,
            "schema": [],
            "ref_options": {},
            "parent_options": [],
            "labels": {"client": {}, "account": {}, "card": {}},
        },
    )

    assert 'hx-put="/entities-htmx/client/3db678b1-1111-2222-3333-444444444444"' in html
    assert 'x-data="' in html
    assert "tags: [&#34;vip \\&#34;quoted\\&#34;&#34;]" in html
    assert "window.PAGE" not in html
    assert "/static/js/entity-form.js" not in html


def test_entity_form_uses_htmx_create_without_static_js() -> None:
    html = render_template(
        "entities/form.html",
        {
            "active": "data",
            "entity_type": "client",
            "title": "Новый клиент",
            "entity_id": None,
            "entity": None,
            "schema": [],
            "ref_options": {},
            "parent_options": [],
            "labels": {"client": {}, "account": {}, "card": {}},
        },
    )

    assert 'hx-post="/entities-htmx/client"' in html
    assert "window.PAGE" not in html
    assert "/static/js/entity-form.js" not in html


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

    page_line = next(line.strip() for line in html.splitlines() if line.strip().startswith("window.PAGE"))

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
            "schema": [SimpleNamespace(name="inn", label="ИНН")],
            "items": [],
            "items_total": 0,
            "ref_options": {},
            "accounts_by_client": {},
            "cards_by_client": {},
            "cards_by_account": {},
            "accounts_by_id": {},
            "labels": {"client": {}, "account": {}, "card": {}},
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

    reference_page_line = next(
        line.strip() for line in reference_html.splitlines() if line.strip().startswith("window.PAGE")
    )

    assert reference_page_line == 'window.PAGE = { entityType: "currency", isReference: true };'
    assert "&#34;" not in reference_page_line
    assert "sort: &#34;created_at&#34;" in entity_html
    assert "direction: &#34;desc&#34;" in entity_html
    assert 'hx-get="/entities-htmx/client/table"' in entity_html
    assert 'hx-trigger="input changed delay:200ms, refresh"' in entity_html
    assert '@input.debounce.200ms="dispatchFilters()"' in entity_html
    assert "window.PAGE" not in entity_html
    assert 'hx-get="/references-htmx/currency/table"' in reference_html
    assert "reference-list.js" not in reference_html


def test_attribute_form_escapes_json_inside_x_data_attribute() -> None:
    html = render_template(
        "partials/attribute_form.html",
        {
            "attribute": None,
            "entity_types": ["client"],
            "selected_entity_type": "client",
            "data_types": ["string", "enum"],
            "reference_types": [],
        },
    )

    assert "x-data=\"{ dataType: &#34;string&#34; }\"" in html


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
    assert "htmx:load" in html
    assert "Alpine.initTree(e.detail.elt)" in html
    assert "Alpine.destroyTree" in html
    assert "htmx:afterSwap" in html
    assert "Alpine.initTree(e.detail.target)" in html
    assert "htmx:oobAfterSwap" in html
    assert "/api/import" not in html


def test_entity_list_has_table_meta_and_detail_drawer() -> None:
    html = render_template(
        "entities/list.html",
        {
            "active": "data",
            "entity_type": "client",
            "title": "Клиенты",
            "schema": [],
            "items": [],
            "items_total": 0,
            "ref_options": {},
            "accounts_by_client": {},
            "cards_by_client": {},
            "cards_by_account": {},
            "accounts_by_id": {},
            "labels": {"client": {}, "account": {}, "card": {}},
        },
    )

    assert 'id="table-meta"' in html
    assert 'id="detail-drawer"' in html
    assert "drawerHasContent: false" in html
    assert 'hx-get="/entities-htmx/client/table"' in html
    assert '<script src="/static/js/entity-list.js">' not in html


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
            "clients": [],
            "labels": {"client": {}, "account": {}, "card": {}},
        },
    )

    assert 'id="role-panels"' in html
    assert "accountOwner: { clientId: '', accountId: '', cardId: '' }" in html
    assert 'hx-post="/templates-htmx/3db678b1-1111-2222-3333-444444444444/fill/render"' in html
    assert 'id="sender-client"' not in html


def test_template_upload_uses_htmx_preview_form() -> None:
    html = render_template(
        "templates_reg/upload.html",
        {
            "active": "templates",
            "llm_active": True,
        },
    )

    assert 'hx-post="/templates-htmx/preview"' in html
    assert 'hx-encoding="multipart/form-data"' in html
    assert 'hx-indicator="#upload-indicator"' in html
    assert 'hx-disabled-elt="find button[type=submit]"' in html
    assert 'id="upload-indicator"' in html
    assert "Обработка LLM" in html
    assert "showFormErrors" in html
    assert "$refs.errors.replaceChildren" not in html
    assert "/static/js/placeholder-editor.js" not in html


def test_template_review_llm_debug_panel_keeps_headings_outside_code_blocks() -> None:
    html = render_template(
        "partials/template_review.html",
        {
            "name": "T",
            "description": "",
            "format": "json",
            "original_content": '{"a":"x"}',
            "llm_meta": {"summary": "ok"},
            "llm_used": True,
            "llm_error": None,
            "llm_debug": {"system_prompt": "system", "user_prompt": "user", "response_text": "raw"},
            "rendered_html": "{}",
            "placeholders": [],
            "catalog": [],
        },
    )

    assert '<div id="llm-debug-panel" class="template-code"' not in html
    assert '<div id="llm-debug-panel" class="llm-debug-panel"' in html
    assert '<pre class="template-code" id="llm-debug-system"' in html
    assert '<pre class="template-code" id="llm-debug-user"' in html
    assert '<pre class="template-code" id="llm-debug-response"' in html
    assert 'id="review-template-code-wrap"' in html
    assert 'id="template-code-wrap"' not in html


def test_template_view_disables_regenerate_when_llm_inactive() -> None:
    html = render_template(
        "templates_reg/view.html",
        {
            "active": "templates",
            "template": SimpleNamespace(
                id="3db678b1-1111-2222-3333-444444444444",
                name="Template",
                description="",
                llm_meta={},
            ),
            "rendered_html": "{}",
            "placeholders": [],
            "catalog": [],
            "llm_active": False,
        },
    )

    assert 'disabled title="LLM не настроена"' in html
    assert 'hx-indicator="#regen-indicator"' in html
    assert 'hx-disabled-elt="this"' in html
    assert 'id="regen-indicator"' in html
    assert "Генерация" in html
    assert html.index('hx-delete="/templates-htmx/') < html.index('id="regen-indicator"')
    assert "Наведите на подсвеченное значение" in html


def test_app_css_defines_htmx_indicator_states() -> None:
    css = (PROJECT_ROOT / "app/static/css/app.css").read_text()

    assert ".htmx-indicator" in css
    assert ".htmx-request .htmx-indicator" in css
    assert ".htmx-indicator.htmx-request" in css
    assert ".btn.htmx-request" in css
    assert ".htmx-request .btn[disabled]" in css
    assert "cursor: wait" in css
    assert "cursor: not-allowed" in css


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

    class FakeTemplates:
        def TemplateResponse(self, request: object, name: str, context: dict[str, object]) -> Any:
            return SimpleNamespace(name=name, context=context)

    install_page_fill_fakes(monkeypatch, template)

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

    class FakeTemplates:
        def TemplateResponse(self, request: object, name: str, context: dict[str, object]) -> Any:
            return SimpleNamespace(name=name, context=context)

    install_page_fill_fakes(monkeypatch, template)

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

    class FakeTemplates:
        def TemplateResponse(self, request: object, name: str, context: dict[str, object]) -> Any:
            return SimpleNamespace(name=name, context=context)

    install_page_fill_fakes(monkeypatch, template)

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
