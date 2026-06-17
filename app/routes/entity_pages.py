from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.routes.deps import SessionDep, TemplatesDep, UnlockedGroupsDep
from app.routes.entities_htmx import build_entity_form_context, build_entity_list_context


def build_entity_pages_router(entity_type: str, segment: str) -> APIRouter:
    """Build the HTML page routes (list / new / edit) for a data entity type."""
    router = APIRouter()

    @router.get(f"/{segment}")
    async def page_list(
        request: Request,
        templates: Jinja2Templates = TemplatesDep,
        session: AsyncSession = SessionDep,
        group_ids: set[uuid.UUID] = UnlockedGroupsDep,
    ) -> Response:
        context = await build_entity_list_context(
            session, entity_type, request, visible_group_ids=group_ids
        )
        return templates.TemplateResponse(request, "entities/list.html", context)

    @router.get(f"/{segment}/new")
    async def page_new(
        request: Request,
        templates: Jinja2Templates = TemplatesDep,
        session: AsyncSession = SessionDep,
        group_ids: set[uuid.UUID] = UnlockedGroupsDep,
    ) -> Response:
        context = await build_entity_form_context(
            session, entity_type, entity_id=None, visible_group_ids=group_ids
        )
        return templates.TemplateResponse(request, "entities/form.html", context)

    @router.get(f"/{segment}/{{entity_id}}/edit")
    async def page_edit(
        entity_id: uuid.UUID,
        request: Request,
        templates: Jinja2Templates = TemplatesDep,
        session: AsyncSession = SessionDep,
        group_ids: set[uuid.UUID] = UnlockedGroupsDep,
    ) -> Response:
        context = await build_entity_form_context(
            session, entity_type, entity_id=entity_id, visible_group_ids=group_ids
        )
        return templates.TemplateResponse(request, "entities/form.html", context)

    return router
