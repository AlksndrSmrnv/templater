from __future__ import annotations

import json
import logging
import ssl
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.llm.runner import llm_service
from app.routes.deps import SessionDep, TemplatesDep
from app.routes.entities_htmx import entity_label
from app.routes.htmx_utils import form_errors_response, form_str, toast_header, validation_errors_response
from app.routes.uow import commit_and_refresh, commit_or_409
from app.schemas.template import TemplateCreate, TemplateFillRequest, TemplateUpdate
from app.services.dynamic_fields import dynamic_token_catalog
from app.services.entities import AccountService, CardService, ClientService
from app.services.placeholders import PlaceholderFiller
from app.services.template_render import render_filled_html, render_template_html
from app.services.templates import (
    TemplateService,
    normalize_placeholders,
    placeholders_have_account_owner,
    template_has_account_owner,
)
from app.utils.errors import DomainError, LLMUnavailable, ValidationFailed

router = APIRouter()
log = logging.getLogger(__name__)

FILL_KEYS = (
    "sender_client_id",
    "sender_account_id",
    "sender_card_id",
    "receiver_client_id",
    "receiver_account_id",
    "receiver_card_id",
    "account_owner_client_id",
    "account_owner_account_id",
    "account_owner_card_id",
)
FILL_ROLES = {"sender", "receiver", "accountOwner"}
FILL_SAVE_ERROR_HEADERS = {"HX-Retarget": "#save-feedback", "HX-Reswap": "innerHTML"}


def _llm_ssl_message(exc: BaseException) -> str:
    return (
        "GigaChat недоступен: проверьте GIGACHAT_CERT_B64/GIGACHAT_KEY_B64. "
        "Сертификат и ключ должны быть PEM-файлами, закодированными в base64. "
        f"Исходная ошибка: {exc}"
    )


def _validate_preview_source(data: TemplateCreate) -> None:
    if not data.content.strip():
        raise ValidationFailed("Пустой шаблон")
    try:
        TemplateService._extract_leaves(data.format, data.content)
    except Exception as exc:
        raise ValidationFailed(f"Шаблон не парсится как {data.format}: {exc}") from exc


async def preview_template(data: TemplateCreate, session: AsyncSession) -> dict[str, Any]:
    _validate_preview_source(data)
    svc = TemplateService(session)
    llm_used = False
    llm_error: str | None = None

    if get_settings().llm_active:
        try:
            async with llm_service() as llm_svc:
                result = await svc.analyze_content(
                    fmt=data.format,
                    original_content=data.content,
                    llm_service=llm_svc,
                )
            llm_used = result.get("llm_debug") is not None
        except LLMUnavailable as exc:
            llm_error = str(exc)
            result = await svc.analyze_content(
                fmt=data.format,
                original_content=data.content,
                llm_service=None,
            )
        except (ssl.SSLError, OSError) as exc:
            llm_error = _llm_ssl_message(exc)
            result = await svc.analyze_content(
                fmt=data.format,
                original_content=data.content,
                llm_service=None,
            )
        except Exception as exc:
            log.warning("LLM template preview failed; falling back to heuristic analysis", exc_info=True)
            llm_error = f"LLM не смогла обработать шаблон; использована эвристика. Исходная ошибка: {exc}"
            result = await svc.analyze_content(
                fmt=data.format,
                original_content=data.content,
                llm_service=None,
            )
    else:
        llm_error = "LLM не настроена; использована эвристика по именам полей."
        result = await svc.analyze_content(
            fmt=data.format,
            original_content=data.content,
            llm_service=None,
        )

    rendered_html = render_template_html(
        SimpleNamespace(
            format=data.format,
            content=result["content"],
            placeholders=result["placeholders"],
        )
    )
    return {
        "name": data.name,
        "description": data.description,
        "format": data.format,
        "original_content": data.content,
        "content": result["content"],
        "placeholders": result["placeholders"],
        "llm_meta": result["llm_meta"],
        "rendered_html": rendered_html,
        "llm_used": llm_used,
        "llm_error": llm_error,
        "llm_debug": result.get("llm_debug"),
        "catalog": await svc.build_field_catalog(),
        "dynamic_tokens": dynamic_token_catalog(),
    }


async def _template_table_context(session: AsyncSession, *, search: str = "") -> dict[str, Any]:
    rows = await TemplateService(session).list_all()
    query = search.strip().lower()
    if query:
        rows = [
            row
            for row in rows
            if query
            in (
                row.name
                + " "
                + row.description
                + " "
                + str((row.llm_meta or {}).get("summary", ""))
                + " "
                + str((row.llm_meta or {}).get("category", ""))
            ).lower()
        ]
    return {"templates_list": rows, "search": search}


async def _template_code_context(session: AsyncSession, template: Any) -> dict[str, Any]:
    return {
        "template": template,
        "rendered_html": render_template_html(template),
        "placeholders": template.placeholders,
        "catalog": await TemplateService(session).build_field_catalog(),
        "dynamic_tokens": dynamic_token_catalog(),
        "llm_active": get_settings().llm_active,
    }


async def _template_editor_context(
    session: AsyncSession,
    template: Any,
    *,
    llm_debug: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = await _template_code_context(session, template)
    context["llm_debug"] = llm_debug
    return context


def _json_form_value(form: Any, key: str, default: Any) -> Any:
    raw = form_str(form, key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationFailed(f"Поле {key} содержит некорректный JSON") from exc


async def _template_create_from_form(request: Request) -> TemplateCreate:
    form = await request.form()
    return TemplateCreate(
        name=form_str(form, "name"),
        description=form_str(form, "description"),
        format=form_str(form, "format") or "json",
        content=form_str(form, "content"),
        analyze_with_llm=False,
        placeholders=_json_form_value(form, "placeholders", []),
        llm_meta=_json_form_value(form, "llm_meta", {}),
    )


async def _template_preview_from_form(request: Request) -> TemplateCreate:
    form = await request.form()
    return TemplateCreate(
        name=form_str(form, "name"),
        description=form_str(form, "description"),
        format=form_str(form, "format") or "json",
        content=form_str(form, "content"),
    )


async def _fill_labels(session: AsyncSession) -> dict[str, dict[str, str]]:
    clients = await ClientService(session).list_all()
    accounts = await AccountService(session).list_all()
    cards = await CardService(session).list_all()
    return {
        "client": {str(item.id): entity_label("client", item) for item in clients},
        "account": {str(item.id): entity_label("account", item) for item in accounts},
        "card": {str(item.id): entity_label("card", item) for item in cards},
    }


def _client_matches(client: Any, query: str) -> bool:
    if not query:
        return True
    attrs = client.attributes or {}
    text = " ".join(
        str(value)
        for value in (
            entity_label("client", client),
            client.description,
            client.id,
            attrs.get("fullName"),
            attrs.get("name"),
            attrs.get("shortName"),
            attrs.get("inn"),
        )
        if value
    ).lower()
    return query.lower() in text


def _check_fill_role(role: str) -> None:
    if role not in FILL_ROLES:
        raise ValidationFailed("Неизвестная роль заполнения")


def _invalid_fill_role_response(
    request: Request,
    templates: Jinja2Templates,
    exc: DomainError,
) -> Response:
    return form_errors_response(
        request,
        templates,
        exc.message,
        details=exc.details,
        status_code=exc.status_code,
    )


def _fill_values_from_form(form: Any) -> dict[str, uuid.UUID | None]:
    values: dict[str, uuid.UUID | None] = {}
    for key in FILL_KEYS:
        raw = form_str(form, key)
        values[key] = uuid.UUID(raw) if raw else None
    return values


async def _fill_request_from_form(request: Request) -> tuple[TemplateFillRequest, dict[str, str]]:
    form = await request.form()
    values = _fill_values_from_form(form)
    raw_values = {key: str(value) for key, value in values.items() if value is not None}
    return TemplateFillRequest(**values), raw_values


async def _render_fill(
    session: AsyncSession,
    template_id: uuid.UUID,
    data: TemplateFillRequest,
) -> tuple[Any, str, str, list[str], list[str]]:
    template = await TemplateService(session).get(template_id)
    rendered, unresolved, changed = await PlaceholderFiller(session).fill_template(
        template,
        sender_client_id=data.sender_client_id,
        sender_account_id=data.sender_account_id,
        sender_card_id=data.sender_card_id,
        receiver_client_id=data.receiver_client_id,
        receiver_account_id=data.receiver_account_id,
        receiver_card_id=data.receiver_card_id,
        account_owner_client_id=data.account_owner_client_id,
        account_owner_account_id=data.account_owner_account_id,
        account_owner_card_id=data.account_owner_card_id,
    )
    rendered_html = render_filled_html(template.format, rendered, changed)
    return template, rendered, rendered_html, unresolved, changed


# ---------- HTML pages ----------

@router.get("/templates")
async def page_list(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return templates.TemplateResponse(
        request,
        "templates_reg/list.html",
        {"active": "templates", **await _template_table_context(session)},
    )


@router.get("/templates/new")
async def page_new(request: Request, templates: Jinja2Templates = TemplatesDep) -> Response:
    return templates.TemplateResponse(request, "templates_reg/upload.html", {"active": "templates"})


@router.get("/templates/{template_id}")
async def page_view(
    template_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    template = await TemplateService(session).get(template_id)
    return templates.TemplateResponse(
        request,
        "templates_reg/view.html",
        {"active": "templates", **await _template_editor_context(session, template)},
    )


@router.get("/templates/{template_id}/fill")
async def page_fill(
    template_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    template = await TemplateService(session).get(template_id)
    clients = await ClientService(session).list_all()
    return templates.TemplateResponse(
        request,
        "templates_reg/fill.html",
        {
            "active": "templates",
            "template": template,
            "clients": clients,
            "labels": await _fill_labels(session),
            "has_account_owner": template_has_account_owner(template),
        },
    )


# ---------- HTMX ----------

@router.get("/templates-htmx/table")
async def htmx_table(
    request: Request,
    search: str = "",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return templates.TemplateResponse(
        request,
        "partials/templates_table.html",
        await _template_table_context(session, search=search),
    )


@router.post("/templates-htmx/preview")
async def htmx_preview(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    try:
        data = await _template_preview_from_form(request)
        context = await preview_template(data, session)
    except ValidationError as exc:
        return validation_errors_response(
            request,
            templates,
            exc,
            status_code=200,
            headers={"HX-Retarget": "#form-errors", "HX-Reswap": "innerHTML"},
        )
    except DomainError as exc:
        return form_errors_response(
            request,
            templates,
            exc.message,
            details=exc.details,
            status_code=200,
            headers={"HX-Retarget": "#form-errors", "HX-Reswap": "innerHTML"},
        )
    return templates.TemplateResponse(
        request,
        "partials/template_review.html",
        context,
        headers={"HX-Trigger": json.dumps({"template-previewed": True})},
    )


@router.post("/templates-htmx")
async def htmx_create(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    try:
        data = await _template_create_from_form(request)
        svc = TemplateService(session)
        template = await svc.create(data)
        template.placeholders = normalize_placeholders(data.placeholders)
        template.llm_meta = {
            **(data.llm_meta or {}),
            "has_account_owner": placeholders_have_account_owner(template.placeholders),
        }
        template.content = svc.regenerate_content(template)
        template = await commit_and_refresh(session, template)
    except ValidationError as exc:
        return validation_errors_response(
            request,
            templates,
            exc,
            status_code=200,
            headers={"HX-Retarget": "#review-errors", "HX-Reswap": "innerHTML"},
        )
    except DomainError as exc:
        return form_errors_response(
            request,
            templates,
            exc.message,
            details=exc.details,
            status_code=200,
            headers={"HX-Retarget": "#review-errors", "HX-Reswap": "innerHTML"},
        )
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": f"/templater/templates/{template.id}",
            "HX-Trigger": toast_header("Шаблон сохранён"),
        },
    )


@router.put("/templates-htmx/{template_id}")
async def htmx_update(
    template_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    form = await request.form()
    try:
        placeholders = _json_form_value(form, "placeholders", [])
        llm_meta = _json_form_value(form, "llm_meta", None)
        if llm_meta is not None and not isinstance(llm_meta, dict):
            raise ValidationFailed("Поле llm_meta должно быть JSON-объектом")
        svc = TemplateService(session)
        if llm_meta is not None:
            await svc.update(template_id, TemplateUpdate(llm_meta=llm_meta))
        template = await svc.update_placeholders(template_id, placeholders)
        template = await commit_and_refresh(session, template)
    except ValidationError as exc:
        return validation_errors_response(request, templates, exc, status_code=200)
    except DomainError as exc:
        return form_errors_response(
            request, templates, exc.message, details=exc.details, status_code=exc.status_code
        )
    return templates.TemplateResponse(
        request,
        "partials/template_editor_response.html",
        await _template_editor_context(session, template),
        headers={"HX-Trigger": toast_header("Шаблон обновлён")},
    )


@router.post("/templates-htmx/{template_id}/regenerate")
async def htmx_regenerate(
    template_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    svc = TemplateService(session)
    template = await svc.get(template_id)
    source = template.original_content or template.content
    llm_debug: dict[str, Any] | None = None
    if get_settings().llm_active:
        try:
            async with llm_service() as llm_svc:
                result = await svc.analyze_content(
                    fmt=template.format,
                    original_content=source,
                    llm_service=llm_svc,
                )
        except Exception:
            log.warning("LLM template regeneration failed; falling back to heuristic analysis", exc_info=True)
            result = await svc.analyze_content(
                fmt=template.format,
                original_content=source,
                llm_service=None,
            )
    else:
        result = await svc.analyze_content(
            fmt=template.format,
            original_content=source,
            llm_service=None,
        )

    llm_debug = result.get("llm_debug")
    preview_template = SimpleNamespace(
        id=template.id,
        name=template.name,
        description=template.description,
        format=template.format,
        content=result["content"],
        original_content=template.original_content,
        placeholders=result["placeholders"],
        llm_meta=result["llm_meta"],
    )
    return templates.TemplateResponse(
        request,
        "partials/template_editor_response.html",
        await _template_editor_context(session, preview_template, llm_debug=llm_debug),
        headers={"HX-Trigger": toast_header("Предпросмотр LLM обновлён. Нажмите «Сохранить изменения»")},
    )


@router.delete("/templates-htmx/{template_id}")
async def htmx_delete(
    template_id: uuid.UUID,
    redirect: bool = False,
    session: AsyncSession = SessionDep,
) -> Response:
    await TemplateService(session).delete(template_id)
    await commit_or_409(session)
    headers = {"HX-Trigger": toast_header("Шаблон удалён")}
    if redirect:
        headers["HX-Redirect"] = "/templater/templates"
    return Response(status_code=204 if redirect else 200, headers=headers)


@router.get("/templates-htmx/{template_id}/fill/clients")
async def htmx_fill_clients(
    template_id: uuid.UUID,
    request: Request,
    role: str,
    q: str = "",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    try:
        _check_fill_role(role)
    except DomainError as exc:
        return _invalid_fill_role_response(request, templates, exc)
    clients = [client for client in await ClientService(session).list_all() if _client_matches(client, q)]
    return templates.TemplateResponse(
        request,
        "partials/fill_clients_list.html",
        {"role": role, "clients": clients, "labels": await _fill_labels(session)},
    )


@router.get("/templates-htmx/{template_id}/fill/accounts")
async def htmx_fill_accounts(
    template_id: uuid.UUID,
    request: Request,
    role: str,
    client_id: uuid.UUID | None = None,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    try:
        _check_fill_role(role)
    except DomainError as exc:
        return _invalid_fill_role_response(request, templates, exc)
    accounts = await AccountService(session).list_all(client_id=client_id) if client_id else []
    return templates.TemplateResponse(
        request,
        "partials/fill_accounts_list.html",
        {
            "role": role,
            "client_id": client_id,
            "accounts": accounts,
            "labels": await _fill_labels(session),
        },
    )


@router.get("/templates-htmx/{template_id}/fill/cards")
async def htmx_fill_cards(
    template_id: uuid.UUID,
    request: Request,
    role: str,
    client_id: uuid.UUID | None = None,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    try:
        _check_fill_role(role)
    except DomainError as exc:
        return _invalid_fill_role_response(request, templates, exc)
    cards = await CardService(session).list_all(client_id=client_id) if client_id else []
    return templates.TemplateResponse(
        request,
        "partials/fill_cards_list.html",
        {
            "role": role,
            "client_id": client_id,
            "cards": cards,
            "labels": await _fill_labels(session),
        },
    )


@router.post("/templates-htmx/{template_id}/fill/render")
async def htmx_fill_render(
    template_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    try:
        data, raw_values = await _fill_request_from_form(request)
        template, rendered, rendered_html, unresolved, changed = await _render_fill(
            session, template_id, data
        )
    except ValueError:
        return form_errors_response(request, templates, "Проверьте выбранные записи")
    return templates.TemplateResponse(
        request,
        "partials/fill_result.html",
        {
            "template": template,
            "content": rendered,
            "rendered_html": rendered_html,
            "unresolved": unresolved,
            # ``changed_json`` / ``unresolved_json`` go straight into the save
            # form as hidden inputs so saving persists exactly the snapshot the
            # user just reviewed — no re-render server-side.
            "changed_json": json.dumps(changed, ensure_ascii=False),
            "unresolved_json": json.dumps(unresolved, ensure_ascii=False),
            "fill_values": raw_values,
        },
    )


@router.post("/templates-htmx/{template_id}/fill/download")
async def htmx_fill_download(
    template_id: uuid.UUID,
    request: Request,
    session: AsyncSession = SessionDep,
) -> StreamingResponse:
    data, _ = await _fill_request_from_form(request)
    template, rendered, _, _, _ = await _render_fill(session, template_id, data)
    payload = rendered.encode("utf-8")
    ext = "xml" if template.format == "xml" else "json"

    def stream() -> Iterator[bytes]:
        yield payload

    return StreamingResponse(
        stream(),
        media_type="application/xml" if ext == "xml" else "application/json",
        headers={"Content-Disposition": f'attachment; filename="filled-{template.id}.{ext}"'},
    )


@router.post("/templates-htmx/{template_id}/fill/save")
async def htmx_fill_save(
    template_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    """Persist the snapshot the user just reviewed in the result panel.

    The save form embeds the rendered ``content`` / ``changed`` / ``unresolved``
    as hidden fields (see ``partials/fill_result.html``). We persist those
    verbatim instead of re-running ``_render_fill`` — otherwise any change to
    the template, the picked client/account/card attributes, or the implicit
    first account/card *between* «Заполнить» and «Сохранить» would silently
    diverge the saved snapshot from what the user actually saw.

    Role IDs are still re-parsed from the form so the audit FK columns get
    validated UUIDs (or NULLs); the snapshot fields are trusted as-is from
    the same response that produced the visible result.
    """

    from app.services.filled_templates import FilledTemplateService

    form = await request.form()
    try:
        data, _raw = await _fill_request_from_form(request)
    except ValueError:
        return form_errors_response(
            request,
            templates,
            "Проверьте выбранные записи",
            status_code=200,
            headers=FILL_SAVE_ERROR_HEADERS,
        )

    filled_content = form_str(form, "content")
    if not filled_content:
        return form_errors_response(
            request,
            templates,
            "Нет результата для сохранения — сначала нажмите «Заполнить».",
            status_code=200,
            headers=FILL_SAVE_ERROR_HEADERS,
        )
    try:
        changed_locations = json.loads(form_str(form, "changed_json") or "[]")
        unresolved = json.loads(form_str(form, "unresolved_json") or "[]")
    except json.JSONDecodeError:
        return form_errors_response(
            request,
            templates,
            "Повреждённые данные результата",
            status_code=200,
            headers=FILL_SAVE_ERROR_HEADERS,
        )
    if not isinstance(changed_locations, list) or not isinstance(unresolved, list):
        return form_errors_response(
            request,
            templates,
            "Повреждённые данные результата",
            status_code=200,
            headers=FILL_SAVE_ERROR_HEADERS,
        )

    template = await TemplateService(session).get(template_id)
    saved = await FilledTemplateService(session).save_from_fill(
        template=template,
        fill_request=data,
        rendered=filled_content,
        changed=[str(x) for x in changed_locations],
        unresolved=[str(x) for x in unresolved],
    )
    saved = await commit_and_refresh(session, saved)
    return Response(
        status_code=204,
        headers={"HX-Redirect": f"/templater/filled-templates/{saved.id}?saved=1"},
    )
