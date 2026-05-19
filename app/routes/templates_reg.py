from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.llm.runner import llm_service
from app.routes.deps import SessionDep, TemplatesDep
from app.schemas.template import (
    TemplateCreate,
    TemplateFillRequest,
    TemplateRead,
    TemplateUpdate,
)
from app.services.placeholders import PlaceholderFiller
from app.services.template_render import render_template_html
from app.services.templates import TemplateService
from app.utils.errors import LLMUnavailable

router = APIRouter()


# ---------- HTML pages ----------

@router.get("/templates")
async def page_list(request: Request, templates: Jinja2Templates = TemplatesDep):
    return templates.TemplateResponse(request, "templates_reg/list.html", {"active": "templates"})


@router.get("/templates/new")
async def page_new(request: Request, templates: Jinja2Templates = TemplatesDep):
    return templates.TemplateResponse(request, "templates_reg/upload.html", {"active": "templates"})


@router.get("/templates/{template_id}")
async def page_view(
    template_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
):
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
):
    template = await TemplateService(session).get(template_id)
    return templates.TemplateResponse(
        request,
        "templates_reg/fill.html",
        {"active": "templates", "template": template},
    )


# ---------- JSON API ----------

@router.get("/api/templates", response_model=list[TemplateRead])
async def api_list(session: AsyncSession = SessionDep):
    items = await TemplateService(session).list()
    return [TemplateRead.model_validate(i, from_attributes=True) for i in items]


@router.get("/api/templates/{template_id}", response_model=TemplateRead)
async def api_get(template_id: uuid.UUID, session: AsyncSession = SessionDep):
    t = await TemplateService(session).get(template_id)
    return TemplateRead.model_validate(t, from_attributes=True)


@router.post("/api/templates", response_model=TemplateRead, status_code=201)
async def api_create(data: TemplateCreate, session: AsyncSession = SessionDep):
    svc = TemplateService(session)
    template = await svc.create(data)
    if data.analyze_with_llm and get_settings().llm_active:
        try:
            async with llm_service() as llm_svc:
                template = await svc.analyze(template, llm_service=llm_svc)
        except LLMUnavailable:
            template = await svc.analyze(template, llm_service=None)
        except Exception:
            template = await svc.analyze(template, llm_service=None)
    else:
        template = await svc.analyze(template, llm_service=None)
    return TemplateRead.model_validate(template, from_attributes=True)


@router.put("/api/templates/{template_id}", response_model=TemplateRead)
async def api_update(
    template_id: uuid.UUID, data: TemplateUpdate, session: AsyncSession = SessionDep
):
    svc = TemplateService(session)
    template = await svc.update(template_id, data)
    # update_placeholders rebuilds content from placeholders against
    # original_content. If the caller replaced content, we already reset
    # placeholders inside update(); running update_placeholders here would
    # silently re-apply stale placeholder positions to the new body.
    if data.placeholders is not None and data.content is None:
        template = await svc.update_placeholders(template_id, data.placeholders)
    return TemplateRead.model_validate(template, from_attributes=True)


@router.delete("/api/templates/{template_id}", status_code=204)
async def api_delete(template_id: uuid.UUID, session: AsyncSession = SessionDep):
    await TemplateService(session).delete(template_id)
    return Response(status_code=204)


@router.post("/api/templates/{template_id}/analyze", response_model=TemplateRead)
async def api_analyze(template_id: uuid.UUID, session: AsyncSession = SessionDep):
    svc = TemplateService(session)
    template = await svc.get(template_id)
    if get_settings().llm_active:
        try:
            async with llm_service() as llm_svc:
                template = await svc.analyze(template, llm_service=llm_svc)
        except LLMUnavailable as exc:
            return JSONResponse(status_code=503, content={"error": "llm_unavailable", "message": str(exc)})
    else:
        template = await svc.analyze(template, llm_service=None)
    return TemplateRead.model_validate(template, from_attributes=True)


@router.get("/api/templates/{template_id}/render")
async def api_render(template_id: uuid.UUID, session: AsyncSession = SessionDep):
    template = await TemplateService(session).get(template_id)
    html = render_template_html(template)
    return {
        "html": html,
        "placeholders": template.placeholders,
        "format": template.format,
    }


@router.get("/api/templates/catalog")
async def api_catalog(session: AsyncSession = SessionDep):
    return await TemplateService(session).build_field_catalog()


@router.post("/api/templates/{template_id}/fill")
async def api_fill(
    template_id: uuid.UUID,
    data: TemplateFillRequest,
    session: AsyncSession = SessionDep,
):
    template = await TemplateService(session).get(template_id)
    filler = PlaceholderFiller(session)
    rendered, unresolved = await filler.fill_template(
        template,
        sender_client_id=data.sender_client_id,
        sender_account_id=data.sender_account_id,
        sender_card_id=data.sender_card_id,
        receiver_client_id=data.receiver_client_id,
        receiver_account_id=data.receiver_account_id,
        receiver_card_id=data.receiver_card_id,
    )
    return {"content": rendered, "format": template.format, "unresolved": unresolved}
