from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.routes.deps import SessionDep, TemplatesDep
from app.routes.entities_htmx import build_entity_form_context, build_entity_list_context

router = APIRouter()


@router.get("/accounts")
async def page_list(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    context = await build_entity_list_context(session, "account", request)
    return templates.TemplateResponse(
        request,
        "entities/list.html",
        context,
    )


@router.get("/accounts/new")
async def page_new(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    context = await build_entity_form_context(session, "account", entity_id=None)
    return templates.TemplateResponse(
        request,
        "entities/form.html",
        context,
    )


@router.get("/accounts/{account_id}/edit")
async def page_edit(
    account_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    context = await build_entity_form_context(session, "account", entity_id=account_id)
    return templates.TemplateResponse(
        request,
        "entities/form.html",
        context,
    )
