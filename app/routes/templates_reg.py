from __future__ import annotations

import logging
import re
import ssl
import uuid
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.llm.runner import llm_service
from app.routes.deps import SessionDep, TemplatesDep
from app.routes.uow import commit_and_refresh, commit_or_409
from app.schemas.template import (
    TemplateCreate,
    TemplateFillRequest,
    TemplateRead,
    TemplateUpdate,
)
from app.services.placeholders import PlaceholderFiller
from app.services.template_render import render_filled_html, render_template_html
from app.services.templates import TemplateService, normalize_placeholders
from app.utils.errors import LLMUnavailable, ValidationFailed

router = APIRouter()
log = logging.getLogger(__name__)
ACCOUNT_OWNER_TOKEN_RE = re.compile(r"\{\{\s*accountOwner\.")


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


def _has_account_owner_placeholders(placeholders: list[dict[str, Any]]) -> bool:
    for item in placeholders:
        suggestion = item.get("suggestion")
        if isinstance(suggestion, str) and suggestion.startswith("accountOwner."):
            return True
        value = item.get("value")
        if isinstance(value, str) and (
            value.startswith("accountOwner.") or ACCOUNT_OWNER_TOKEN_RE.search(value)
        ):
            return True
    return False


# ---------- HTML pages ----------

@router.get("/templates")
async def page_list(request: Request, templates: Jinja2Templates = TemplatesDep) -> Response:
    return templates.TemplateResponse(request, "templates_reg/list.html", {"active": "templates"})


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
    rendered = render_template_html(template)
    return templates.TemplateResponse(
        request,
        "templates_reg/view.html",
        {
            "active": "templates",
            "template": template,
            "rendered_html": rendered,
        },
    )


@router.get("/templates/{template_id}/fill")
async def page_fill(
    template_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    template = await TemplateService(session).get(template_id)
    has_account_owner = _has_account_owner_placeholders(template.placeholders or [])
    return templates.TemplateResponse(
        request,
        "templates_reg/fill.html",
        {"active": "templates", "template": template, "has_account_owner": has_account_owner},
    )


# ---------- JSON API ----------

@router.get("/api/templates", response_model=list[TemplateRead])
async def api_list(session: AsyncSession = SessionDep) -> list[TemplateRead]:
    items = await TemplateService(session).list_all()
    return [TemplateRead.model_validate(i, from_attributes=True) for i in items]


@router.get("/api/templates/catalog")
async def api_catalog(session: AsyncSession = SessionDep) -> list[dict[str, str]]:
    return await TemplateService(session).build_field_catalog()


@router.get("/api/templates/{template_id}", response_model=TemplateRead)
async def api_get(template_id: uuid.UUID, session: AsyncSession = SessionDep) -> TemplateRead:
    t = await TemplateService(session).get(template_id)
    return TemplateRead.model_validate(t, from_attributes=True)


@router.post("/api/templates", response_model=TemplateRead, status_code=201)
async def api_create(data: TemplateCreate, session: AsyncSession = SessionDep) -> TemplateRead:
    svc = TemplateService(session)
    template = await svc.create(data)
    if data.placeholders is not None:
        template.placeholders = normalize_placeholders(data.placeholders)
        template.llm_meta = data.llm_meta or {}
        template.content = svc.regenerate_content(template)
        template = await commit_and_refresh(session, template)
        return TemplateRead.model_validate(template, from_attributes=True)

    template = await commit_and_refresh(session, template)
    if data.analyze_with_llm and get_settings().llm_active:
        try:
            async with llm_service() as llm_svc:
                template = await svc.analyze(template, llm_service=llm_svc)
        except LLMUnavailable:
            template = await svc.analyze(template, llm_service=None)
        except Exception:
            log.warning("LLM template analysis failed; falling back to heuristic analysis", exc_info=True)
            template = await svc.analyze(template, llm_service=None)
    else:
        template = await svc.analyze(template, llm_service=None)
    template = await commit_and_refresh(session, template)
    return TemplateRead.model_validate(template, from_attributes=True)


@router.post("/api/templates/preview")
async def api_preview(data: TemplateCreate, session: AsyncSession = SessionDep) -> dict[str, Any]:
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
            llm_used = True
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
        "format": data.format,
        "original_content": data.content,
        "content": result["content"],
        "placeholders": result["placeholders"],
        "llm_meta": result["llm_meta"],
        "rendered_html": rendered_html,
        "llm_used": llm_used,
        "llm_error": llm_error,
    }


@router.put("/api/templates/{template_id}", response_model=TemplateRead)
async def api_update(
    template_id: uuid.UUID, data: TemplateUpdate, session: AsyncSession = SessionDep
) -> TemplateRead:
    svc = TemplateService(session)
    template = await svc.update(template_id, data)
    # update_placeholders rebuilds content from placeholders against
    # original_content. If the caller replaced content, we already reset
    # placeholders inside update(); running update_placeholders here would
    # silently re-apply stale placeholder positions to the new body.
    if data.placeholders is not None and data.content is None:
        template = await svc.update_placeholders(template_id, data.placeholders)
    template = await commit_and_refresh(session, template)
    return TemplateRead.model_validate(template, from_attributes=True)


@router.delete("/api/templates/{template_id}", status_code=204)
async def api_delete(template_id: uuid.UUID, session: AsyncSession = SessionDep) -> Response:
    await TemplateService(session).delete(template_id)
    await commit_or_409(session)
    return Response(status_code=204)


@router.post("/api/templates/{template_id}/analyze", response_model=TemplateRead)
async def api_analyze(
    template_id: uuid.UUID, session: AsyncSession = SessionDep
) -> TemplateRead | JSONResponse:
    svc = TemplateService(session)
    template = await svc.get(template_id)
    if get_settings().llm_active:
        try:
            async with llm_service() as llm_svc:
                template = await svc.analyze(template, llm_service=llm_svc)
        except LLMUnavailable as exc:
            return JSONResponse(status_code=503, content={"error": "llm_unavailable", "message": str(exc)})
        except (ssl.SSLError, OSError) as exc:
            return JSONResponse(
                status_code=503,
                content={"error": "llm_unavailable", "message": _llm_ssl_message(exc)},
            )
    else:
        template = await svc.analyze(template, llm_service=None)
    template = await commit_and_refresh(session, template)
    return TemplateRead.model_validate(template, from_attributes=True)


@router.get("/api/templates/{template_id}/render")
async def api_render(template_id: uuid.UUID, session: AsyncSession = SessionDep) -> dict[str, Any]:
    template = await TemplateService(session).get(template_id)
    html = render_template_html(template)
    return {
        "html": html,
        "placeholders": template.placeholders,
        "format": template.format,
    }


@router.post("/api/templates/{template_id}/fill")
async def api_fill(
    template_id: uuid.UUID,
    data: TemplateFillRequest,
    session: AsyncSession = SessionDep,
) -> dict[str, Any]:
    template = await TemplateService(session).get(template_id)
    filler = PlaceholderFiller(session)
    rendered, unresolved, changed = await filler.fill_template(
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
    html = render_filled_html(template.format, rendered, changed)
    return {"content": rendered, "html": html, "format": template.format, "unresolved": unresolved}
