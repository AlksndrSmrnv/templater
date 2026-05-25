from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.routes.deps import SessionDep, TemplatesDep
from app.routes.entities_htmx import build_entity_form_context, build_entity_list_context

router = APIRouter()


@router.get("/cards")
async def page_list(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    context = await build_entity_list_context(session, "card", request)
    return templates.TemplateResponse(
        request,
        "entities/list.html",
        context,
    )


@router.get("/cards/new")
async def page_new(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    context = await build_entity_form_context(session, "card", entity_id=None)
    return templates.TemplateResponse(
        request,
        "entities/form.html",
        context,
    )


@router.get("/cards/{card_id}/edit")
async def page_edit(
    card_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    context = await build_entity_form_context(session, "card", entity_id=card_id)
    return templates.TemplateResponse(
        request,
        "entities/form.html",
        context,
    )
