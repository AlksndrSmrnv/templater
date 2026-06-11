"""Routes for the «Заполненные шаблоны» workspace.

Pages:
- ``GET /filled-templates`` — workspace: folder tree on the left, detail panel
  on the right. ``?open=<uuid>`` opens that item's panel on load.
- ``GET /filled-templates/{id}`` — standalone detail page (direct links).

HTMX:
- ``GET /filled-templates-htmx/tree?search=...`` — tree partial.
- ``POST /filled-templates-htmx/folders`` / ``.../folders/rename`` /
  ``DELETE .../folders`` — folder CRUD; each returns the refreshed tree.
- ``POST /filled-templates-htmx/move`` — drag-and-drop move/reorder.
- ``GET /filled-templates-htmx/{id}/panel`` — detail panel partial.
- ``DELETE /filled-templates-htmx/{id}`` — delete; ``?panel=1`` returns the
  empty-state for the panel and triggers a tree refresh.

Raw:
- ``GET /filled-templates/{id}/raw`` — ``text/plain``-ish content for the
  Copy button (fetch + clipboard) and for direct curl/Postman use. With
  ``?download=1`` returns ``Content-Disposition: attachment``.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.entity import ClientRepository
from app.routes.deps import SessionDep, TemplatesDep
from app.routes.htmx_utils import form_str, parse_json_path, parse_uuid_list, toast_header
from app.routes.uow import commit_or_409
from app.services.filled_templates import FilledTemplateService, iter_role_labels
from app.services.template_render import render_filled_html
from app.utils.errors import DomainError

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


async def _tree_response(
    request: Request,
    templates: Jinja2Templates,
    session: AsyncSession,
    *,
    search: str = "",
    headers: dict[str, str] | None = None,
) -> Response:
    context = await FilledTemplateService(session).build_tree(search=search)
    return templates.TemplateResponse(
        request,
        "partials/filled_tree.html",
        context,
        headers=headers,
    )


async def _detail_context(
    session: AsyncSession, filled_id: uuid.UUID
) -> dict[str, Any]:
    """Context shared by the standalone view page and the workspace panel."""

    item = await FilledTemplateService(session).get(filled_id)
    rendered_html = render_filled_html(
        item.format, item.filled_content, item.changed_locations or []
    )
    role_client_ids: dict[str, uuid.UUID | None] = {
        "sender": item.sender_client_id,
        "receiver": item.receiver_client_id,
        "accountOwner": item.account_owner_client_id,
    }
    alive = await _alive_client_ids(session, list(role_client_ids.values()))
    return {
        "ft": item,
        "rendered_html": rendered_html,
        "role_rows": iter_role_labels(item),
        "role_client_ids": role_client_ids,
        "alive_client_ids": alive,
    }


# ---------- HTML pages ----------


@router.get("/filled-templates")
async def page_list(
    request: Request,
    search: str = "",
    open: str = "",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    # ``open`` (a filled-template id) opens that item's panel on load — used by
    # the post-save redirect. Validate it as a UUID so an attacker-controlled
    # query string can't be reflected into the page.
    open_filled_id = ""
    raw_open = open.strip()
    if raw_open:
        try:
            open_filled_id = str(uuid.UUID(raw_open))
        except ValueError:
            open_filled_id = ""
    tree_context = await FilledTemplateService(session).build_tree(search=search)
    return templates.TemplateResponse(
        request,
        "filled_templates/workspace.html",
        {
            "active": "templates",
            "open_filled_id": open_filled_id,
            **tree_context,
        },
    )


@router.get("/filled-templates/{filled_id}")
async def page_view(
    filled_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    context = await _detail_context(session, filled_id)
    return templates.TemplateResponse(
        request,
        "filled_templates/view.html",
        {"active": "templates", "standalone": True, **context},
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


@router.get("/filled-templates-htmx/tree")
async def htmx_tree(
    request: Request,
    search: str = "",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return await _tree_response(request, templates, session, search=search)


# Folder routes MUST be declared before ``/filled-templates-htmx/{filled_id}``:
# FastAPI captures ``{filled_id}`` as a string at routing time and only
# validates it as a UUID afterwards, so the literal ``folders`` would otherwise
# match the parametrized route and 422 before reaching these handlers.
@router.post("/filled-templates-htmx/folders")
async def htmx_create_folder(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    form = await request.form()
    parent = parse_json_path(form_str(form, "parent"))
    name = form_str(form, "name")
    try:
        await FilledTemplateService(session).create_folder(parent, name)
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return await _tree_response(
            request,
            templates,
            session,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    return await _tree_response(
        request,
        templates,
        session,
        headers={"HX-Trigger": toast_header(f"Папка «{name.strip()}» создана")},
    )


@router.post("/filled-templates-htmx/folders/rename")
async def htmx_rename_folder(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    form = await request.form()
    path = parse_json_path(form_str(form, "path"))
    name = form_str(form, "name")
    try:
        await FilledTemplateService(session).rename_folder(path, name)
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return await _tree_response(
            request,
            templates,
            session,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    return await _tree_response(
        request,
        templates,
        session,
        headers={"HX-Trigger": toast_header("Папка переименована")},
    )


@router.delete("/filled-templates-htmx/folders")
async def htmx_delete_folder(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    # htmx 2.x encodes DELETE params (hx-vals) in the URL query string (config
    # ``methodsThatUseUrlParams`` defaults to ['get','delete']); fall back to
    # the form body for safety.
    raw = request.query_params.get("path", "")
    if not raw:
        raw = form_str(await request.form(), "path")
    path = parse_json_path(raw)
    try:
        await FilledTemplateService(session).delete_folder(path)
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return await _tree_response(
            request,
            templates,
            session,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    return await _tree_response(
        request,
        templates,
        session,
        headers={"HX-Trigger": toast_header("Папка удалена")},
    )


@router.post("/filled-templates-htmx/move")
async def htmx_move(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    form = await request.form()
    try:
        filled_id = uuid.UUID(form_str(form, "filled_id"))
    except ValueError:
        return await _tree_response(
            request,
            templates,
            session,
            headers={"HX-Trigger": toast_header("Некорректный запрос", toast_type="error")},
        )
    folder = parse_json_path(form_str(form, "folder"))
    order = parse_uuid_list(form_str(form, "order"))
    try:
        await FilledTemplateService(session).move_filled(filled_id, folder, order)
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return await _tree_response(
            request,
            templates,
            session,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    return await _tree_response(request, templates, session)


@router.get("/filled-templates-htmx/{filled_id}/panel")
async def htmx_panel(
    filled_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    context = await _detail_context(session, filled_id)
    return templates.TemplateResponse(
        request,
        "partials/filled_panel.html",
        {"standalone": False, **context},
    )


@router.delete("/filled-templates-htmx/{filled_id}")
async def htmx_delete(
    filled_id: uuid.UUID,
    request: Request,
    redirect: bool = False,
    panel: bool = False,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    await FilledTemplateService(session).delete(filled_id)
    await commit_or_409(session)
    if panel:
        # Deleted from the workspace panel: swap in the empty-state and let the
        # ``refresh-filled-tree`` trigger reload the tree (it includes the
        # current search input, so the filter is preserved).
        return templates.TemplateResponse(
            request,
            "partials/filled_panel_empty.html",
            {},
            headers={
                "HX-Trigger": toast_header(
                    "Заполненный шаблон удалён", refresh_filled_tree=True
                )
            },
        )
    headers = {"HX-Trigger": toast_header("Заполненный шаблон удалён")}
    if redirect:
        headers["HX-Redirect"] = "/templater/filled-templates"
    return Response(status_code=204 if redirect else 200, headers=headers)
