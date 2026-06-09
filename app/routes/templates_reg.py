from __future__ import annotations

import json
import logging
import ssl
import uuid
from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.runner import llm_service
from app.repositories.filled_template import FilledTemplateRepository
from app.routes.deps import SessionDep, TemplatesDep
from app.routes.entities_htmx import entity_label
from app.routes.htmx_utils import form_errors_response, form_str, toast_header, validation_errors_response
from app.routes.uow import commit_and_refresh, commit_or_409
from app.schemas.template import TemplateCreate, TemplateFillRequest, TemplateUpdate
from app.services.collections import CollectionService
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
from app.utils import walker
from app.utils.errors import DomainError, LLMResponseError, LLMUnavailable, ValidationFailed
from app.utils.signing import sign_processed, verify_processed

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


def _llm_failure_text(exc: BaseException) -> str:
    """User-facing message for a failed LLM call.

    The GigaChat client re-raises a plain ``Exception`` after exhausting
    retries; SSL/OS errors get the cert-specific hint. Used by the regenerate /
    process routes to render a form error instead of bubbling up a 500.
    """

    if isinstance(exc, (ssl.SSLError, OSError)):
        return _llm_ssl_message(exc)
    return f"LLM не смогла обработать шаблон: {exc}"


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

    # LLM is required. A missing/broken configuration or a failed call surfaces
    # as a DomainError that ``htmx_preview`` renders as a form error — the tool
    # has no heuristic-only mode.
    try:
        async with llm_service(session=session) as llm_svc:
            result = await svc.analyze_content(
                fmt=data.format,
                original_content=data.content,
                llm_service=llm_svc,
            )
    except DomainError:
        raise
    except (ssl.SSLError, OSError) as exc:
        raise LLMUnavailable(_llm_ssl_message(exc)) from exc
    except Exception as exc:
        log.warning("LLM template preview failed", exc_info=True)
        raise LLMResponseError(f"LLM не смогла обработать шаблон: {exc}") from exc

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
        # Server-side proof that the LLM analysed this exact content into this
        # exact llm_meta; the review form echoes it back so `htmx_create` can
        # mark the template processed without trusting a client-supplied flag.
        "llm_proof": sign_processed(data.content, result["llm_meta"]),
        "rendered_html": rendered_html,
        "llm_used": result.get("llm_debug") is not None,
        "llm_debug": result.get("llm_debug"),
        "catalog": await svc.build_field_catalog(),
        "dynamic_tokens": dynamic_token_catalog(),
        # Carried through to the review form's hidden fields so the saved request
        # lands in the collection/folder the user started from.
        "preset_collection_id": str(data.collection_id) if data.collection_id else "",
        "preset_folder_path": list(data.folder_path or []),
    }


def _is_parsable(template: Any) -> bool:
    """True when the body parses as its declared format (so LLM analysis and
    fill are possible). Imported GET/urlencoded/empty/invalid bodies are not.

    Uses the walker directly (not ``TemplateService``) so it is independent of
    that class being monkeypatched in tests.
    """

    source = template.original_content or template.content
    if not source or not source.strip():
        return False
    try:
        if template.format == "json":
            walker.walk_json(source)
        elif template.format == "xml":
            walker.walk_xml(source)
        else:
            return False
    except Exception:
        return False
    return True


async def _template_code_context(session: AsyncSession, template: Any) -> dict[str, Any]:
    return {
        "template": template,
        "rendered_html": render_template_html(template),
        "placeholders": template.placeholders,
        "catalog": await TemplateService(session).build_field_catalog(),
        "dynamic_tokens": dynamic_token_catalog(),
        "parsable": _is_parsable(template),
    }


async def _template_panel_context(session: AsyncSession, template: Any) -> dict[str, Any]:
    context = await _template_code_context(session, template)
    context["headers"] = template.headers or []
    context["has_account_owner"] = template_has_account_owner(template)
    context["filled_links"] = await FilledTemplateRepository(session).list_by_template(template.id)
    # Show the LLM prompts/response captured at the last analysis (incl. bulk
    # collection processing) so it can be inspected from the collections menu.
    context["llm_debug"] = getattr(template, "llm_debug", None)
    return context


async def _template_editor_context(
    session: AsyncSession,
    template: Any,
    *,
    llm_debug: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = await _template_code_context(session, template)
    # Fall back to the debug stored on the template when no fresh one is passed
    # (e.g. opening the editor after a bulk collection run).
    context["llm_debug"] = llm_debug if llm_debug is not None else getattr(template, "llm_debug", None)
    return context


def _json_form_value(form: Any, key: str, default: Any) -> Any:
    raw = form_str(form, key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationFailed(f"Поле {key} содержит некорректный JSON") from exc


def _placement_from_form(form: Any) -> tuple[uuid.UUID | None, list[str]]:
    """Read the optional collection/folder placement carried by the new-template
    form (set by the "+ запрос" buttons in the collections tree)."""

    raw_collection = form_str(form, "collection_id").strip()
    collection_id: uuid.UUID | None = None
    if raw_collection:
        try:
            collection_id = uuid.UUID(raw_collection)
        except ValueError:
            collection_id = None
    folder_raw = _json_form_value(form, "folder_path", [])
    folder_path = (
        [str(seg).strip() for seg in folder_raw if str(seg).strip()]
        if isinstance(folder_raw, list)
        else []
    )
    return collection_id, folder_path


async def _template_create_from_form(request: Request) -> TemplateCreate:
    form = await request.form()
    collection_id, folder_path = _placement_from_form(form)
    return TemplateCreate(
        name=form_str(form, "name"),
        description=form_str(form, "description"),
        format=form_str(form, "format") or "json",
        content=form_str(form, "content"),
        analyze_with_llm=False,
        placeholders=_json_form_value(form, "placeholders", []),
        llm_meta=_json_form_value(form, "llm_meta", {}),
        collection_id=collection_id,
        folder_path=folder_path,
    )


async def _template_preview_from_form(request: Request) -> TemplateCreate:
    form = await request.form()
    collection_id, folder_path = _placement_from_form(form)
    return TemplateCreate(
        name=form_str(form, "name"),
        description=form_str(form, "description"),
        format=form_str(form, "format") or "json",
        content=form_str(form, "content"),
        collection_id=collection_id,
        folder_path=folder_path,
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
    template: str = "",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    tree = await CollectionService(session).build_workspace_tree()
    # ``template`` (a template id) opens that request's panel on load — used by
    # deep links from the assistant, filled templates and the create flow, now
    # that the standalone editor page is retired. Validate it as a UUID so an
    # attacker-controlled query string can't be reflected into the page.
    open_template_id = ""
    raw_open = template.strip()
    if raw_open:
        try:
            open_template_id = str(uuid.UUID(raw_open))
        except ValueError:
            open_template_id = ""
    return templates.TemplateResponse(
        request,
        "templates_reg/workspace.html",
        {"active": "templates", "open_template_id": open_template_id, **tree},
    )


@router.get("/templates/new")
async def page_new(
    request: Request,
    collection_id: str = "",
    folder: str = "",
    templates: Jinja2Templates = TemplatesDep,
) -> Response:
    # ``folder`` is a JSON array of path segments coming from the "+ запрос"
    # buttons in the collections tree; surfaced to the form as hidden fields so
    # the created request lands in the right collection/folder.
    folder_path: list[str] = []
    if folder.strip():
        try:
            value = json.loads(folder)
            if isinstance(value, list):
                folder_path = [str(seg).strip() for seg in value if str(seg).strip()]
        except ValueError:
            folder_path = []
    return templates.TemplateResponse(
        request,
        "templates_reg/upload.html",
        {
            "active": "templates",
            "preset_collection_id": collection_id.strip(),
            "preset_folder_path": folder_path,
            "preset_folder_label": " / ".join(folder_path),
        },
    )


@router.get("/templates/{template_id}")
async def page_view(template_id: uuid.UUID) -> RedirectResponse:
    # The standalone editor page is retired — all editing happens in the
    # collections workspace panel. Keep this URL working (bookmarks, old links)
    # by redirecting to the workspace with that template's panel auto-opened.
    return RedirectResponse(
        url=f"/templater/templates?template={template_id}",
        status_code=307,
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
            # Optional preselection carried in the query string (e.g. from the
            # home-page LLM assistant link) — seeds the role pickers.
            "preset": _fill_preset_from_query(request),
        },
    )


def _fill_preset_from_query(request: Request) -> dict[str, dict[str, str]]:
    """Build the role→{clientId, accountId, cardId} preselection from query params.

    Keys mirror ``FILL_KEYS`` (``sender_client_id`` …); values are passed through
    verbatim and consumed by ``fill.html`` to seed the Alpine state.
    """

    qp = getattr(request, "query_params", {})
    return {
        "sender": {
            "clientId": qp.get("sender_client_id", ""),
            "accountId": qp.get("sender_account_id", ""),
            "cardId": qp.get("sender_card_id", ""),
        },
        "receiver": {
            "clientId": qp.get("receiver_client_id", ""),
            "accountId": qp.get("receiver_account_id", ""),
            "cardId": qp.get("receiver_card_id", ""),
        },
        "accountOwner": {
            "clientId": qp.get("account_owner_client_id", ""),
            "accountId": qp.get("account_owner_account_id", ""),
            "cardId": qp.get("account_owner_card_id", ""),
        },
    }


# ---------- HTMX ----------

@router.get("/templates-htmx/tree")
async def htmx_tree(
    request: Request,
    search: str = "",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    context = await CollectionService(session).build_workspace_tree(search=search)
    return templates.TemplateResponse(request, "partials/collections_tree.html", context)


@router.get("/templates-htmx/{template_id}/panel")
async def htmx_panel(
    template_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    template = await TemplateService(session).get(template_id)
    return templates.TemplateResponse(
        request,
        "partials/template_panel.html",
        await _template_panel_context(session, template),
    )


async def _reprocess_panel(
    template_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates,
    session: AsyncSession,
    *,
    action: Callable[[TemplateService, Any, Any], Awaitable[Any]],
    toast: str,
) -> Response:
    """Run an LLM (re)processing ``action`` on a template and re-render its panel.

    Shared by the combined "process" and the granular meta-only / fields-only
    reprocess routes so they all report LLM failures into ``#panel-errors`` and
    persist via ``commit_and_refresh`` identically.
    """

    svc = TemplateService(session)
    template = await svc.get(template_id)
    panel_error_headers = {"HX-Retarget": "#panel-errors", "HX-Reswap": "innerHTML"}
    # Scope the broad catch to the LLM analysis only — a persistence failure in
    # commit_and_refresh must surface as a real error, not be masked as an LLM failure.
    try:
        async with llm_service(session=session) as llm_svc:
            await action(svc, template, llm_svc)
    except DomainError as exc:
        return form_errors_response(
            request,
            templates,
            exc.message,
            details=exc.details,
            status_code=200,
            headers=panel_error_headers,
        )
    except Exception as exc:
        # The GigaChat client raises a plain Exception after exhausting retries;
        # surface it as a form error rather than a global 500.
        log.warning("LLM processing failed for template %s", template_id, exc_info=True)
        await session.rollback()
        return form_errors_response(
            request,
            templates,
            _llm_failure_text(exc),
            status_code=200,
            headers=panel_error_headers,
        )
    template = await commit_and_refresh(session, template)
    return templates.TemplateResponse(
        request,
        "partials/template_panel.html",
        await _template_panel_context(session, template),
        headers={"HX-Trigger": toast_header(toast)},
    )


@router.post("/templates-htmx/{template_id}/process-llm")
async def htmx_process_llm(
    template_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return await _reprocess_panel(
        template_id,
        request,
        templates,
        session,
        action=lambda svc, template, llm_svc: svc.analyze_and_persist(
            template, llm_service=llm_svc
        ),
        toast="Шаблон обработан LLM",
    )


@router.post("/templates-htmx/{template_id}/regenerate-meta")
async def htmx_regenerate_meta(
    template_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    """Reprocess ONLY the metadata (summary) of an already-processed template."""

    return await _reprocess_panel(
        template_id,
        request,
        templates,
        session,
        action=lambda svc, template, llm_svc: svc.regenerate_meta_and_persist(
            template, llm_service=llm_svc
        ),
        toast="Метаинформация обновлена",
    )


@router.post("/templates-htmx/{template_id}/regenerate-fields")
async def htmx_regenerate_fields(
    template_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    """Reprocess ONLY the template body (placeholders/mapping), keeping the meta."""

    return await _reprocess_panel(
        template_id,
        request,
        templates,
        session,
        action=lambda svc, template, llm_svc: svc.regenerate_fields_and_persist(
            template, llm_service=llm_svc
        ),
        toast="Шаблон обработан заново",
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
        # `request.form()` is cached by Starlette, so re-reading it here is free.
        llm_proof = form_str(await request.form(), "llm_proof")
        svc = TemplateService(session)
        template = await svc.create(data)
        template.placeholders = normalize_placeholders(data.placeholders)
        template.content = svc.regenerate_content(template)
        # `import_status` is a server-managed flag, never client input — strip any
        # value the POST tried to smuggle in so it can only ever be set below,
        # after the proof verifies. (Preview never emits it, so legitimate
        # payloads are unaffected.)
        client_meta = {k: v for k, v in (data.llm_meta or {}).items() if k != "import_status"}
        template.llm_meta = {
            **client_meta,
            "has_account_owner": placeholders_have_account_owner(template.placeholders),
        }
        # Mark the template processed only when the server itself vouches — via
        # the HMAC proof issued during preview — that it LLM-analysed this exact
        # content into this exact metadata. A client-crafted POST can't forge a
        # valid proof (nor swap in fake analysis under a stolen one), so it can't
        # flip `import_status` and bypass `_require_processed`. Without a valid
        # proof the panel simply offers full «Обработать LLM», as before.
        if _is_parsable(template) and verify_processed(data.content, client_meta, llm_proof):
            template.llm_meta["import_status"] = "processed"
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
            "HX-Redirect": f"/templater/templates?template={template.id}",
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
    except DomainError as exc:
        # E.g. the template body doesn't parse as its declared format (imported
        # GET/urlencoded request) — surface readable feedback instead of the
        # global JSON error response.
        return form_errors_response(
            request, templates, exc.message, details=exc.details, status_code=200
        )
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
