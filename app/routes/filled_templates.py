"""Routes for the «Заполненные шаблоны» page.

Pages:
- ``GET /filled-templates`` — list with debounced search.
- ``GET /filled-templates/{id}`` — detail with green highlighting and copy button.

HTMX:
- ``GET /filled-templates-htmx/table?search=...`` — table partial.
- ``DELETE /filled-templates-htmx/{id}`` — delete row, fires toast.

Raw:
- ``GET /filled-templates/{id}/raw`` — ``text/plain``-ish content for the
  Copy button (fetch + clipboard) and for direct curl/Postman use. With
  ``?download=1`` returns ``Content-Disposition: attachment``.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.entity import ClientRepository
from app.routes.deps import SessionDep, TemplatesDep
from app.routes.htmx_utils import toast_header
from app.routes.uow import commit_or_409
from app.services.filled_templates import FilledTemplateService, iter_role_labels
from app.services.template_render import render_filled_html

router = APIRouter()


async def _alive_client_ids(
    session: AsyncSession, ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """Return the subset of ``ids`` that still exist in ``clients``."""

    unique = [cid for cid in ids if cid is not None]
    if not unique:
        return set()
    rows = await ClientRepository(session).get_many(unique)
    return {row.id for row in rows}


def _media_type(fmt: str) -> str:
    return "application/xml" if fmt == "xml" else "application/json"


# ---------- HTML pages ----------


@router.get("/filled-templates")
async def page_list(
    request: Request,
    search: str = "",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    items = await FilledTemplateService(session).list_all(search=search)
    return templates.TemplateResponse(
        request,
        "filled_templates/list.html",
        {
            "active": "templates",
            "search": search,
            "filled_templates": items,
        },
    )


@router.get("/filled-templates/{filled_id}")
async def page_view(
    filled_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    svc = FilledTemplateService(session)
    item = await svc.get(filled_id)
    rendered_html = render_filled_html(
        item.format, item.filled_content, item.changed_locations or []
    )
    role_rows = iter_role_labels(item)
    role_client_ids: dict[str, uuid.UUID | None] = {
        "sender": item.sender_client_id,
        "receiver": item.receiver_client_id,
        "accountOwner": item.account_owner_client_id,
    }
    alive = await _alive_client_ids(session, list(role_client_ids.values()))
    return templates.TemplateResponse(
        request,
        "filled_templates/view.html",
        {
            "active": "templates",
            "ft": item,
            "rendered_html": rendered_html,
            "role_rows": role_rows,
            "role_client_ids": role_client_ids,
            "alive_client_ids": alive,
        },
    )


@router.get("/filled-templates/{filled_id}/raw")
async def page_raw(
    filled_id: uuid.UUID,
    download: bool = False,
    session: AsyncSession = SessionDep,
) -> Response:
    item = await FilledTemplateService(session).get(filled_id)
    headers: dict[str, str] = {}
    if download:
        ext = "xml" if item.format == "xml" else "json"
        headers["Content-Disposition"] = f'attachment; filename="filled-{item.id}.{ext}"'
    return PlainTextResponse(
        item.filled_content,
        media_type=_media_type(item.format),
        headers=headers,
    )


# ---------- HTMX ----------


@router.get("/filled-templates-htmx/table")
async def htmx_table(
    request: Request,
    search: str = "",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    items = await FilledTemplateService(session).list_all(search=search)
    return templates.TemplateResponse(
        request,
        "partials/filled_templates_table.html",
        {"filled_templates": items},
    )


@router.delete("/filled-templates-htmx/{filled_id}")
async def htmx_delete(
    filled_id: uuid.UUID,
    redirect: bool = False,
    session: AsyncSession = SessionDep,
) -> Response:
    await FilledTemplateService(session).delete(filled_id)
    await commit_or_409(session)
    headers = {"HX-Trigger": toast_header("Заполненный шаблон удалён")}
    if redirect:
        headers["HX-Redirect"] = "/filled-templates"
    return Response(status_code=204 if redirect else 200, headers=headers)
