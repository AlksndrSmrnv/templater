from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.routes.deps import SessionDep, TemplatesDep
from app.routes.entities_htmx import build_entity_form_context, build_entity_list_context

router = APIRouter()


# ---------- HTML pages ----------

@router.get("/clients")
async def page_list(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    context = await build_entity_list_context(session, "client", request)
    return templates.TemplateResponse(
        request,
        "entities/list.html",
        context,
    )


@router.get("/clients/new")
async def page_new(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    context = await build_entity_form_context(session, "client", entity_id=None)
    return templates.TemplateResponse(
        request,
        "entities/form.html",
        context,
    )


@router.get("/clients/{client_id}/edit")
async def page_edit(
    client_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    context = await build_entity_form_context(session, "client", entity_id=client_id)
    return templates.TemplateResponse(
        request,
        "entities/form.html",
        context,
    )
