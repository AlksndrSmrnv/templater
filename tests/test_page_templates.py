from __future__ import annotations

import re
import uuid
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.routes import templates_reg
from app.routes.deps import get_templates

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def render_template(name: str, context: dict[str, object]) -> str:
    return get_templates().env.get_template(name).render(context)


class StartTagCollector(HTMLParser):
    def __init__(self, tag_name: str) -> None:
        super().__init__()
        self.tag_name = tag_name
        self.tags: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == self.tag_name:
            self.tags.append(dict(attrs))


def start_tags(html: str, tag_name: str) -> list[dict[str, str | None]]:
    collector = StartTagCollector(tag_name)
    collector.feed(html)
    return collector.tags


def start_tag_by_id(html: str, tag_name: str, element_id: str) -> dict[str, str | None]:
    matches = [tag for tag in start_tags(html, tag_name) if tag.get("id") == element_id]
    assert len(matches) == 1
    return matches[0]


class FirstStartTagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.first: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.first is None:
            self.first = tag


def first_start_tag(html: str) -> str | None:
    collector = FirstStartTagCollector()
    collector.feed(html)
    return collector.first


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

    assert 'hx-put="/templater/entities-htmx/client/3db678b1-1111-2222-3333-444444444444"' in html
    assert 'x-data="' in html
    assert "tags: [&#34;vip \\&#34;quoted\\&#34;&#34;]" in html
    assert "window.PAGE" not in html
    assert "/templater/static/js/entity-form.js" not in html


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

    assert 'hx-post="/templater/entities-htmx/client"' in html
    assert "window.PAGE" not in html
    assert "/templater/static/js/entity-form.js" not in html


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
            "accounts_by_client": {},
            "cards_by_client": {},
            "cards_by_account": {},
            "accounts_by_id": {},
            "labels": {"client": {}, "account": {}, "card": {}},
        },
    )

    assert "sort: &#34;created_at&#34;" in entity_html
    assert "direction: &#34;desc&#34;" in entity_html
    assert 'hx-get="/templater/entities-htmx/client/table"' in entity_html
    assert 'hx-trigger="input changed delay:200ms, refresh"' in entity_html
    assert '@input.debounce.200ms="dispatchFilters()"' in entity_html
    assert "window.PAGE" not in entity_html


def test_entities_table_refresh_renders_rows_before_oob_meta() -> None:
    """On htmx refresh (oob_meta=True) the response must START with a table <tr>,
    not the OOB <div> blocks. htmx 2.0 parses the response inside a <template> and
    picks the insertion mode from the first tag — a leading <div> makes the parser
    drop all <tr>/<td>, collapsing the table into one column. See plan / htmx
    makeFragment behaviour."""
    html = render_template(
        "partials/entities_table.html",
        {
            "oob_meta": True,
            "entity_type": "client",
            "items": [
                SimpleNamespace(
                    id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
                    description="Acme",
                    attributes={},
                    tags=[],
                    created_at="2026-01-01",
                )
            ],
            "items_total": 1,
            "schema": [],
            "relation_columns": ("accounts", "cards"),
            "accounts_by_client": {},
            "cards_by_client": {},
            "ref_options": {},
            "labels": {"client": {}, "account": {}, "card": {}},
            "page": 1,
            "pages": 1,
            "has_prev": False,
            "has_next": False,
        },
    )

    assert 'id="table-meta"' in html
    assert 'id="table-pagination"' in html
    # The VERY FIRST tag of the response must be <tr>. htmx picks the parse mode
    # from the leading tag, so any other leading element (e.g. a <div>) would make
    # the browser drop the table rows. Checking the first tag (not just relative
    # order vs. #table-meta) guards against re-introducing any leading wrapper.
    assert first_start_tag(html) == "tr"


def test_attribute_form_escapes_json_inside_x_data_attribute() -> None:
    html = render_template(
        "partials/attribute_form.html",
        {
            "attribute": None,
            "entity_types": ["client"],
            "selected_entity_type": "client",
            "data_types": ["string", "enum"],
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

    assert 'hx-post="/templater/import-htmx"' in html
    assert 'hx-encoding="multipart/form-data"' in html
    assert "htmx.org@2.0.4" in html
    assert "alpinejs@3.14.1" in html
    assert "htmx:load" in html
    assert "Alpine.initTree(e.detail.elt)" in html
    assert "Alpine.destroyTree" not in html
    assert "htmx:afterSwap" not in html
    assert "htmx:oobAfterSwap" not in html
    assert "/api/import" not in html


def test_filled_template_view_copy_fetch_uses_templater_prefix() -> None:
    html = render_template(
        "filled_templates/view.html",
        {
            "active": "filled_templates",
            "ft": SimpleNamespace(
                id="3db678b1-1111-2222-3333-444444444444",
                name="Filled",
                message_template_id=None,
                template_name_snapshot=None,
                created_at=None,
                unresolved=[],
            ),
            "role_rows": [],
            "role_client_ids": {},
            "alive_client_ids": set(),
            "rendered_html": "{}",
        },
    )

    assert "fetch('/templater/filled-templates/3db678b1-1111-2222-3333-444444444444/raw')" in html
    assert "fetch('/filled-templates/" not in html


def test_unresolved_notice_groups_by_role_with_hints() -> None:
    html = render_template(
        "partials/unresolved_notice.html",
        {
            "unresolved": [
                "sender.account.number",
                "receiver.card.number",
                "accountOwner.firstName",
            ]
        },
    )

    # Callout header + actionable section appear.
    assert "Не удалось подставить часть параметров" in html
    assert "Что делать" in html

    # Paths are grouped under human-readable role labels.
    assert "Отправитель" in html
    assert "Получатель" in html
    assert "Владелец счёта" in html

    # Each path gets a hint matched to its second segment.
    assert "не выбран счёт для этой роли" in html
    assert "не выбрана карта для этой роли" in html
    assert "нет данных у клиента" in html

    # Raw dot-paths are still shown for advanced users.
    assert "sender.account.number" in html
    assert "receiver.card.number" in html
    assert "accountOwner.firstName" in html


def test_unresolved_notice_empty_renders_nothing() -> None:
    html = render_template("partials/unresolved_notice.html", {"unresolved": []})

    assert "fill-warning" not in html
    assert html.strip() == ""


def test_filled_templates_table_copy_fetch_uses_templater_prefix() -> None:
    html = render_template(
        "partials/filled_templates_table.html",
        {
            "filled_templates": [
                SimpleNamespace(
                    id="3db678b1-1111-2222-3333-444444444444",
                    name="Filled",
                    unresolved=[],
                    message_template_id=None,
                    template_name_snapshot=None,
                    format="json",
                    role_labels_snapshot={},
                    created_at=None,
                )
            ],
            "truncated": False,
        },
    )

    assert "fetch('/templater/filled-templates/3db678b1-1111-2222-3333-444444444444/raw')" in html
    assert "fetch('/filled-templates/" not in html


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
    assert 'hx-get="/templater/entities-htmx/client/table"' in html
    assert 'x-effect="document.body.style.overflow = drawerOpen ? \'hidden\' : \'\'"' in html
    assert '<script src="/templater/static/js/entity-list.js">' not in html


def test_entity_detail_preserves_whitespace_only_for_text_values() -> None:
    html = render_template(
        "partials/entity_detail.html",
        {
            "entity_type": "client",
            "item": SimpleNamespace(
                id="3db678b1-1111-2222-3333-444444444444",
                description="Line 1\nLine 2",
                attributes={
                    "notes": "Free\ntext",
                    "code": "ABC",
                },
                tags=["vip", "manual"],
                created_at="2026-05-28",
                updated_at="2026-05-28",
            ),
            "schema": [
                SimpleNamespace(name="notes", label="Notes", data_type="text"),
                SimpleNamespace(name="code", label="Code", data_type="string"),
            ],
            "accounts_by_client": {},
            "cards_by_client": {},
            "cards_by_account": {},
            "accounts_by_id": {},
            "labels": {"client": {}, "account": {}, "card": {}},
            "ref_options": {},
        },
    )

    assert html.count('class="detail-value detail-value--text"') == 2
    # Tags and string-typed fields render as a plain detail-value (no whitespace
    # preservation). Match independently of HTML indentation/formatting.
    assert re.search(
        r'<div class="detail-label">Теги</div>\s*<div class="detail-value">', html
    )
    assert re.search(
        r'<div class="detail-label">Code</div>\s*<div class="detail-value">', html
    )
    # Card-grid layout: fields live in a .detail-grid and long fields span full width.
    assert 'class="detail-grid"' in html
    assert "detail-cell--wide" in html


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
            "preset": {
                role: {"clientId": "", "accountId": "", "cardId": ""}
                for role in ("sender", "receiver", "accountOwner")
            },
        },
    )

    assert 'id="role-panels"' in html
    # Preset strings are tojson'd then HTML-escaped (&#34;) so they stay inside
    # the double-quoted x-data attribute instead of terminating it early.
    assert "accountOwner: { clientId: &#34;&#34;, accountId: &#34;&#34;, cardId: &#34;&#34; }" in html
    assert 'hx-post="/templater/templates-htmx/3db678b1-1111-2222-3333-444444444444/fill/render"' in html
    assert 'id="sender-client"' not in html
    assert "pickFromList(event, role, kind)" in html
    assert "event.stopPropagation();" in html
    assert "this.selectClient(role, id);" in html
    assert "this.selectAccount(role, id);" in html
    assert "this.selectCard(role, id);" in html
    assert "unknown selector kind" in html
    assert "kind selector exists but no dispatch case" in html
    assert "if (this.state[role].clientId === id) return;" in html
    for role in ("sender", "receiver", "accountOwner"):
        client_list = start_tag_by_id(html, "div", f"{role}-clients")
        account_list = start_tag_by_id(html, "div", f"{role}-accounts")
        card_list = start_tag_by_id(html, "div", f"{role}-cards")

        assert client_list.get("@click") == f"pickFromList($event, '{role}', 'client')"
        assert account_list.get("@click") == f"pickFromList($event, '{role}', 'account')"
        assert card_list.get("@click") == f"pickFromList($event, '{role}', 'card')"
        assert client_list.get("role") == "listbox"
        assert account_list.get("role") == "listbox"
        assert card_list.get("role") == "listbox"
        assert account_list.get("hx-params") == "client_id"
        assert card_list.get("hx-params") == "client_id"


def test_template_fill_page_preset_cannot_break_out_of_x_data_attribute() -> None:
    # A query-controlled preset value containing a double quote must not
    # terminate the double-quoted x-data attribute or inject a new attribute.
    html = render_template(
        "templates_reg/fill.html",
        {
            "active": "templates",
            "template": SimpleNamespace(id="x", name="Fill", format="json"),
            "has_account_owner": False,
            "clients": [],
            "labels": {"client": {}, "account": {}, "card": {}},
            "preset": {
                "sender": {"clientId": 'x" onmouseover="alert(1)', "accountId": "", "cardId": ""},
                "receiver": {"clientId": "", "accountId": "", "cardId": ""},
                "accountOwner": {"clientId": "", "accountId": "", "cardId": ""},
            },
        },
    )
    # No raw breakout sequence; the payload's quotes are JSON-escaped (\") and
    # then HTML-escaped (&#34;), so a backslash precedes each escaped quote.
    assert 'x" onmouseover="alert(1)' not in html
    assert "onmouseover=\\&#34;alert(1)" in html


def test_template_fill_partials_use_data_attributes_for_delegated_selection() -> None:
    client_id = "11111111-1111-1111-1111-111111111111"
    account_id = "22222222-2222-2222-2222-222222222222"
    card_id = "33333333-3333-3333-3333-333333333333"
    clients_html = render_template(
        "partials/fill_clients_list.html",
        {
            "role": "sender",
            "clients": [SimpleNamespace(id=client_id)],
            "labels": {"client": {client_id: "Client One"}, "account": {}, "card": {}},
        },
    )
    accounts_html = render_template(
        "partials/fill_accounts_list.html",
        {
            "role": "sender",
            "client_id": client_id,
            "accounts": [SimpleNamespace(id=account_id, description="Primary account")],
            "labels": {"client": {}, "account": {account_id: "Account One"}, "card": {}},
        },
    )
    cards_html = render_template(
        "partials/fill_cards_list.html",
        {
            "role": "sender",
            "client_id": client_id,
            "cards": [SimpleNamespace(id=card_id, account_id=account_id, description="Primary card")],
            "labels": {"client": {}, "account": {account_id: "Account One"}, "card": {card_id: "Card One"}},
        },
    )

    client_button = start_tags(clients_html, "button")[0]
    account_button = start_tags(accounts_html, "button")[0]
    card_button = start_tags(cards_html, "button")[0]

    assert client_button.get("data-client-id") == client_id
    assert account_button.get("data-account-id") == account_id
    assert card_button.get("data-card-id") == card_id
    assert client_button.get("role") == "option"
    assert account_button.get("role") == "option"
    assert card_button.get("role") == "option"
    assert client_button.get(":aria-selected") == f"state.sender.clientId === '{client_id}'"
    assert account_button.get(":aria-selected") == f"state.sender.accountId === '{account_id}'"
    assert card_button.get(":aria-selected") == f"state.sender.cardId === '{card_id}'"
    assert "@click" not in client_button
    assert "@click" not in account_button
    assert "@click" not in card_button


def test_template_upload_uses_htmx_preview_form() -> None:
    html = render_template(
        "templates_reg/upload.html",
        {
            "active": "templates",
            "llm_active": True,
        },
    )

    assert 'hx-post="/templater/templates-htmx/preview"' in html
    assert 'hx-encoding="multipart/form-data"' in html
    assert 'hx-indicator="#upload-indicator"' in html
    assert 'hx-disabled-elt="find button[type=submit]"' in html
    assert 'id="upload-indicator"' in html
    assert "Обработка LLM" in html
    assert "showFormErrors" in html
    assert "$refs.errors.replaceChildren" not in html
    assert "/templater/static/js/placeholder-editor.js" not in html


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


def test_template_edit_llm_panel_shows_llm_debug_toggle_when_present() -> None:
    # The standalone editor page is retired; the LLM meta panel + debug toggle now
    # live inside the collections workspace panel (template_edit_llm_panel.html).
    html = render_template(
        "partials/template_edit_llm_panel.html",
        {
            "template": SimpleNamespace(
                id="3db678b1-1111-2222-3333-444444444444",
                name="Template",
                description="",
                llm_meta={"summary": "ok"},
            ),
            "llm_debug": {
                "system_prompt": "sys",
                "user_prompt": "usr",
                "response_text": "resp",
            },
        },
    )

    assert 'id="template-llm-panel"' in html
    assert 'name="llm_meta"' in html
    assert "Показать запрос и ответ LLM" in html
    assert 'id="llm-debug-system"' in html
    assert 'id="llm-debug-user"' in html
    assert 'id="llm-debug-response"' in html


def test_app_css_defines_htmx_indicator_states() -> None:
    css = (PROJECT_ROOT / "app/static/css/app.css").read_text()

    assert ".htmx-indicator" in css
    assert ".htmx-request .htmx-indicator" in css
    assert ".htmx-indicator.htmx-request" in css
    assert ".btn.htmx-request" in css
    assert ".htmx-request .btn[disabled]" in css
    assert "cursor: wait" in css
    assert "cursor: not-allowed" in css
    assert ".dropdown li.dropdown-section-header" in css


def test_template_code_partial_seeds_dynamic_tokens_for_picker() -> None:
    # The placeholder dropdown reads its dynamic-token list from a hidden seed
    # input. Both routes that render this partial (preview + view) must pass
    # ``dynamic_tokens`` so the picker offers rqUID/operUID/rqTm/channelDateTime
    # as explicit options for any field — see services.dynamic_fields.
    from app.services.dynamic_fields import dynamic_token_catalog

    html = render_template(
        "partials/template_code.html",
        {
            "rendered_html": "{}",
            "placeholders": [],
            "catalog": [],
            "dynamic_tokens": dynamic_token_catalog(),
        },
    )

    assert 'x-ref="dynamicTokensSeed"' in html
    # Token names appear inside the JSON-encoded seed value.
    for token_name in ("rqUID", "operUID", "rqTm", "channelDateTime"):
        assert token_name in html
    # Visible label and the picker handler are wired into the dropdown UI.
    assert "Динамические параметры" in html
    assert "chooseDynamic(token)" in html
    assert "dropdown-section-header" in html


def test_template_code_partial_stops_click_propagation_on_placeholder() -> None:
    # The dropdown's @click.outside lives on the document and fires for any
    # click that bubbles past it. The placeholder click MUST stopPropagation
    # so the same trusted click that opens the picker doesn't immediately
    # close it via click.outside. Without stopPropagation, Alpine sets
    # dropdownIdx=2 → renders dropdown → click.outside on the still-bubbling
    # event fires → resets dropdownIdx=null in the same task. Picker never
    # appears to the user.
    html = render_template(
        "partials/template_code.html",
        {"rendered_html": "{}", "placeholders": [], "catalog": [], "dynamic_tokens": []},
    )
    assert "$event.stopPropagation()" in html
    # And it must only stop when an actual placeholder was clicked — clicks
    # on empty pre area must still bubble so click.outside can close an
    # already-open picker.
    assert "if (span) { $event.stopPropagation()" in html


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


# ---------- Collections workspace rendering ----------

def _tree_template(name: str, *, method: str = "POST", status: str = "imported") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        http_method=method,
        llm_meta={"import_status": status},
    )


def test_collections_tree_renders_collection_folders_and_items() -> None:
    item = _tree_template("A2A Transfer")
    tree = {"folders": {"Transfers": {"folders": {}, "templates": [item]}}, "templates": []}
    context = {
        "collection_nodes": [
            {"collection": SimpleNamespace(id=uuid.uuid4(), name="Demo Bank"), "count": 1, "tree": tree}
        ],
        "ungrouped_tree": {"folders": {}, "templates": []},
        "ungrouped_count": 0,
        "search": "",
    }
    html = render_template("partials/collections_tree.html", context)
    assert "Demo Bank" in html
    assert "Transfers" in html
    assert "A2A Transfer" in html
    # leaf wires up the panel load
    items = [tag for tag in start_tags(html, "li") if tag.get("hx-get")]
    assert any("/panel" in (tag.get("hx-get") or "") for tag in items)


def test_collections_tree_shows_ungrouped_and_empty_state() -> None:
    ungrouped_item = _tree_template("Manual template", status="imported")
    context = {
        "collection_nodes": [],
        "ungrouped_tree": {"folders": {}, "templates": [ungrouped_item]},
        "ungrouped_count": 1,
        "search": "",
    }
    html = render_template("partials/collections_tree.html", context)
    assert "Без коллекции" in html
    assert "Manual template" in html

    empty = render_template(
        "partials/collections_tree.html",
        {"collection_nodes": [], "ungrouped_tree": {"folders": {}, "templates": []}, "ungrouped_count": 0, "search": ""},
    )
    assert "Пока нет шаблонов" in empty


def test_collections_tree_renders_root_folders_block() -> None:
    item = _tree_template("Loose grouped")
    context = {
        "collection_nodes": [],
        "root_tree": {"folders": {"Inbox": {"folders": {}, "templates": [item]}}, "templates": []},
        "ungrouped_tree": {"folders": {}, "templates": []},
        "ungrouped_count": 0,
        "search": "",
    }
    html = render_template("partials/collections_tree.html", context)
    assert "Папки" in html
    assert "Inbox" in html
    # Root folder ops target the dedicated /collections/root/folders endpoints.
    forms = [tag for tag in start_tags(html, "form") if tag.get("hx-post")]
    assert any(
        (tag.get("hx-post") or "").endswith("/collections/root/folders") for tag in forms
    )
    assert "Пока нет шаблонов" not in html  # root folders count as content


def test_collections_tree_process_button_enabled_when_llm_inactive() -> None:
    item = _tree_template("A2A Transfer")
    tree = {"folders": {}, "templates": [item]}
    context = {
        "collection_nodes": [
            {"collection": SimpleNamespace(id=uuid.uuid4(), name="Demo Bank"), "count": 1, "tree": tree}
        ],
        "ungrouped_tree": {"folders": {}, "templates": []},
        "ungrouped_count": 0,
        "search": "",
        "llm_active": False,  # button stays enabled; a failed LLM call surfaces as a form error
    }
    html = render_template("partials/collections_tree.html", context)
    process = [tag for tag in start_tags(html, "button") if "/process-llm" in (tag.get("hx-post") or "")]
    assert process and "disabled" not in process[0]


def _panel_template(**overrides: Any) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        name="A2A Transfer",
        description="Перевод",
        http_method="POST",
        url="https://api.bank.test/transfer/a2a",
        format="json",
        content='{"amount": 100}',
        llm_meta={"summary": "Перевод со счёта на счёт"},
        placeholders=[],
        headers=[
            {"key": "RqUID", "value": "{{rqUID}}", "mode": "dynamic", "original": "abc", "disabled": False},
            {"key": "Content-Type", "value": "application/json", "mode": "literal", "original": "application/json", "disabled": False},
        ],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _panel_context(template: SimpleNamespace, *, parsable: bool, filled_links: list[Any] | None = None) -> dict[str, Any]:
    return {
        "template": template,
        "rendered_html": "{}",
        "placeholders": template.placeholders,
        "catalog": [],
        "dynamic_tokens": [],
        "llm_active": True,
        "headers": template.headers,
        "parsable": parsable,
        "has_account_owner": False,
        "filled_links": filled_links or [],
    }


def test_template_panel_parsable_shows_headers_body_and_process_button() -> None:
    template = _panel_template()
    html = render_template("partials/template_panel.html", _panel_context(template, parsable=True))
    assert "RqUID" in html
    assert "{{rqUID}}" in html
    assert "динамический" in html
    assert "Обработать LLM" in html
    assert "template-editor-form" in html  # interactive editor present
    assert "Заполнить" in html  # fill available for parsable templates
    # process-llm button targets the center panel
    buttons = [tag for tag in start_tags(html, "button") if tag.get("hx-post")]
    assert any("/process-llm" in (tag.get("hx-post") or "") for tag in buttons)


def test_template_panel_unparsable_disables_process_and_hides_editor() -> None:
    template = _panel_template(http_method="GET", content="", url="https://api.bank.test/health", headers=[])
    html = render_template("partials/template_panel.html", _panel_context(template, parsable=False))
    assert "template-editor-form" not in html
    assert "Заполнить" not in html  # fill hidden — body can't be substituted
    process = [tag for tag in start_tags(html, "button") if "/process-llm" in (tag.get("hx-post") or "")]
    assert process and "disabled" in process[0]


def test_template_panel_process_button_enabled_when_parsable() -> None:
    # The process button is gated only by parsability (LLM is required and
    # assumed configured); it must be enabled for a parsable template.
    html = render_template("partials/template_panel.html", _panel_context(_panel_template(), parsable=True))
    process = [tag for tag in start_tags(html, "button") if "/process-llm" in (tag.get("hx-post") or "")]
    assert process and "disabled" not in process[0]


def test_template_panel_unprocessed_shows_single_process_button() -> None:
    # Default fixture has no import_status → the single combined "process" button.
    html = render_template("partials/template_panel.html", _panel_context(_panel_template(), parsable=True))
    posts = [tag.get("hx-post") or "" for tag in start_tags(html, "button")]
    assert any("/process-llm" in p for p in posts)
    assert not any("/regenerate-meta" in p for p in posts)
    assert not any("/regenerate-fields" in p for p in posts)


def test_template_panel_processed_shows_split_reprocess_buttons() -> None:
    template = _panel_template(llm_meta={"summary": "S", "import_status": "processed"})
    html = render_template("partials/template_panel.html", _panel_context(template, parsable=True))
    posts = [tag.get("hx-post") or "" for tag in start_tags(html, "button")]
    # Once processed: two granular buttons replace the combined one.
    assert any("/regenerate-meta" in p for p in posts)
    assert any("/regenerate-fields" in p for p in posts)
    assert not any("/process-llm" in p for p in posts)


def test_template_panel_has_delete_button_and_no_editor_link() -> None:
    # Delete moved into the panel; the standalone editor page link is gone.
    html = render_template("partials/template_panel.html", _panel_context(_panel_template(), parsable=True))
    deletes = [tag.get("hx-delete") or "" for tag in start_tags(html, "button")]
    assert any("/templates-htmx/" in d for d in deletes)
    assert "Удалить" in html
    assert ">Редактор</a>" not in html


def _workspace_context(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "active": "templates",
        "root_tree": {"folders": {}, "templates": []},
        "collection_nodes": [],
        "ungrouped_tree": {"folders": {}, "templates": []},
        "ungrouped_count": 0,
        "search": "",
    }
    base.update(overrides)
    return base


def test_workspace_opens_panel_for_deep_link() -> None:
    tid = "3db678b1-1111-2222-3333-444444444444"
    html = render_template(
        "templates_reg/workspace.html", _workspace_context(open_template_id=tid)
    )
    assert f"/templater/templates-htmx/{tid}/panel" in html
    assert 'hx-trigger="load"' in html
    # selected is seeded as a JS string (HTML-escaped by forceescape).
    assert "&#34;" in html


def test_workspace_escapes_malicious_open_template_id() -> None:
    # Defence-in-depth: even if a non-UUID value reached the template, it must be
    # neutralised, not reflected into the Alpine x-data / hx-get attributes.
    html = render_template(
        "templates_reg/workspace.html",
        _workspace_context(open_template_id='"});alert(1)//'),
    )
    assert '"});alert' not in html  # no raw double-quote breakout
    assert "&#34;" in html  # x-data value HTML-escaped
    assert "%22" in html  # panel URL urlencoded


def test_template_panel_lists_filled_links() -> None:
    template = _panel_template()
    filled = SimpleNamespace(id=uuid.uuid4(), name="A2A — Иванов — 29.05.2026", created_at=__import__("datetime").datetime(2026, 5, 29, 12, 0))
    html = render_template("partials/template_panel.html", _panel_context(template, parsable=True, filled_links=[filled]))
    assert "A2A — Иванов — 29.05.2026" in html
    assert f"/templater/filled-templates/{filled.id}" in html


def test_home_shows_assistant_form_when_llm_active() -> None:
    html = render_template("home.html", {"active": "home", "llm_active": True})
    assert 'name="prompt"' in html
    assert 'hx-post="/templater/assistant/compose"' in html
    assert 'id="assistant-result"' in html
    assert 'id="assistant-errors"' in html


def test_home_hides_assistant_form_when_llm_inactive() -> None:
    html = render_template("home.html", {"active": "home", "llm_active": False})
    assert 'name="prompt"' not in html
    assert "не настроена" in html


def test_assistant_result_partial_renders_picks_and_link() -> None:
    html = render_template(
        "partials/assistant_result.html",
        {
            "template": SimpleNamespace(
                id="3db678b1-1111-2222-3333-444444444444", name="Перевод A2A"
            ),
            "roles": [
                {
                    "role": "sender",
                    "title": "Отправитель",
                    "client_label": "Иванов",
                    "traits": "резидент",
                    "account_label": "40817...111",
                    "account_currency": "USD",
                    "card_label": "5536...000",
                },
                {
                    "role": "receiver",
                    "title": "Получатель",
                    "client_label": "Петров",
                    "traits": "недееспособный",
                    "account_label": "40817...222",
                    "account_currency": "RUB",
                    "card_label": None,
                },
            ],
            "rendered": "{}",
            "rendered_html": "{&quot;amount&quot;: 100}",
            "unresolved": [],
            "fill_qs": "sender_client_id=abc&receiver_client_id=def",
        },
    )
    assert "Перевод A2A" in html
    # The & in the query string is HTML-escaped to &amp; inside the href.
    assert (
        '/templater/templates/3db678b1-1111-2222-3333-444444444444/fill'
        '?sender_client_id=abc&amp;receiver_client_id=def' in html
    )
    assert "Отправитель" in html and "Иванов" in html
    assert "недееспособный" in html
    assert "USD" in html


def test_assistant_result_partial_warns_on_unresolved() -> None:
    html = render_template(
        "partials/assistant_result.html",
        {
            "template": SimpleNamespace(id="x", name="T"),
            "roles": [],
            "rendered": "{}",
            "rendered_html": "{}",
            "unresolved": ["sender.account.number"],
            "fill_qs": "",
        },
    )
    assert "Не заполнены поля" in html
    assert "sender.account.number" in html


# ---------------------------------------------------------------------------
# Project badges: explicit colored chip on tree items, panel and filled views.
# ---------------------------------------------------------------------------


def test_collections_tree_renders_project_badge() -> None:
    item = _tree_template("A2A Transfer")
    item.project = SimpleNamespace(id=uuid.uuid4(), name="Альфа", color="#112233")
    context = {
        "collection_nodes": [],
        "ungrouped_tree": {"folders": {}, "templates": [item]},
        "ungrouped_count": 1,
        "search": "",
    }
    html = render_template("partials/collections_tree.html", context)
    assert 'class="project-badge"' in html
    assert "Альфа" in html
    assert "background: #112233" in html


def test_collections_tree_tolerates_template_without_project() -> None:
    # Doubles (and pre-refresh rows) without the relationship render no badge.
    item = _tree_template("Bare")
    context = {
        "collection_nodes": [],
        "ungrouped_tree": {"folders": {}, "templates": [item]},
        "ungrouped_count": 1,
        "search": "",
    }
    html = render_template("partials/collections_tree.html", context)
    assert "project-badge" not in html


def test_template_panel_shows_project_badge_and_reassign_select() -> None:
    project = SimpleNamespace(id=uuid.uuid4(), name="Альфа", color="#112233")
    other = SimpleNamespace(id=uuid.uuid4(), name="Бета", color="#445566")
    template = _panel_template(project=project)
    context = _panel_context(template, parsable=True)
    context["projects"] = [project, other]
    html = render_template("partials/template_panel.html", context)

    assert 'class="project-badge"' in html
    assert "background: #112233" in html
    forms = [tag for tag in start_tags(html, "form") if "/project" in (tag.get("hx-post") or "")]
    assert len(forms) == 1
    # current project preselected in the reassign select
    options = start_tags(html, "option")
    selected = [o for o in options if "selected" in o and o.get("value") == str(project.id)]
    assert selected


def test_template_panel_filled_links_show_project_snapshot_badge() -> None:
    template = _panel_template(project=SimpleNamespace(id=uuid.uuid4(), name="Альфа", color="#112233"))
    filled = SimpleNamespace(
        id=uuid.uuid4(),
        name="A2A — 29.05.2026",
        created_at=__import__("datetime").datetime(2026, 5, 29, 12, 0),
        project_name_snapshot="Альфа",
        project_color_snapshot="#112233",
    )
    context = _panel_context(template, parsable=True, filled_links=[filled])
    context["projects"] = []
    html = render_template("partials/template_panel.html", context)
    assert html.count('class="project-badge"') >= 2  # panel header + filled link


def test_filled_template_view_shows_project_row_from_snapshot() -> None:
    base = dict(
        id="3db678b1-1111-2222-3333-444444444444",
        name="Filled",
        message_template_id=None,
        template_name_snapshot=None,
        created_at=None,
        unresolved=[],
    )
    context = {
        "active": "filled_templates",
        "role_rows": [],
        "role_client_ids": {},
        "alive_client_ids": set(),
        "rendered_html": "{}",
    }

    with_project = render_template(
        "filled_templates/view.html",
        {**context, "ft": SimpleNamespace(**base, project_name_snapshot="Альфа", project_color_snapshot="#112233")},
    )
    assert "Проект" in with_project
    assert 'class="project-badge"' in with_project
    assert "background: #112233" in with_project

    without_project = render_template(
        "filled_templates/view.html",
        {**context, "ft": SimpleNamespace(**base)},
    )
    assert "Проект" in without_project
    assert "project-badge" not in without_project


def test_workspace_tree_listens_for_refresh_tree_event() -> None:
    # The reassign endpoint fires refresh-tree so the sidebar badge updates
    # without a page reload — the tree container must subscribe to it.
    html = render_template("templates_reg/workspace.html", _workspace_context())
    trees = [tag for tag in start_tags(html, "div") if tag.get("id") == "collections-tree"]
    assert len(trees) == 1
    assert trees[0].get("hx-trigger") == "refresh-tree from:body"
    assert trees[0].get("hx-get") == "/templater/templates-htmx/tree"
