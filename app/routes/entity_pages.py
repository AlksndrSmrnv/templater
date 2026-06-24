from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.routes.deps import SessionDep, TemplatesDep, UnlockedGroupsDep
from app.routes.entities_htmx import build_entity_form_context, build_entity_list_context


def _parse_open_id(request: Request) -> uuid.UUID | None:
    """Read the ``?open=<uuid>`` deep-link target from the query string.

    Returns ``None`` (and lets the page render normally) for a missing or
    malformed value so a bad cross-link never 500s the list page.
    """

    raw = request.query_params.get("open")
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError, AttributeError):
        return None


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
        open_id = _parse_open_id(request)
        context = await build_entity_list_context(
            session, entity_type, request, visible_group_ids=group_ids, open_id=open_id
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
