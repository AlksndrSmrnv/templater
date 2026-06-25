"""Routes for «Цепочка запросов» — chains of REST requests built from filled
templates, shown inline in the «Заполненные шаблоны» workspace.

The chain itself is persisted in the DB (``request_chains`` /
``request_chain_steps``). The browser component (``partials/chain_panel.html``)
renders steps seeded by the server, lets the user bind fields from earlier
steps' responses into later requests (``{{ $N.path }}`` tokens, highlighted
purple) and «sends» each step. Sending is a STUB — no network request is made
(the real tool will arrive over an API later); ``POST /send-htmx/execute`` just
echoes the step's editable example response back.

Chain CRUD endpoints return the refreshed filled-templates tree (chains live in
that tree); step endpoints return the refreshed chain panel.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RequestChain, RequestChainStep
from app.routes.deps import SessionDep, TemplatesDep, UnlockedGroupsDep
from app.routes.htmx_utils import form_str, parse_json_path, parse_uuid_list, toast_header
from app.routes.uow import commit_or_409
from app.services.filled_templates import FilledTemplateService
from app.services.request_chain import RequestChainService
from app.utils.errors import DomainError

router = APIRouter()


def _toast_only(
    message: str, toast_type: str = "success", *, refresh_filled_tree: bool = False
) -> Response:
    """A 200 with no body that shows a toast and swaps nothing (``HX-Reswap:
    none``). Used on error paths where re-rendering the panel could itself fail
    (e.g. the chain just became invisible), so HTMX must keep the current DOM."""

    return Response(
        status_code=200,
        headers={
            "HX-Trigger": toast_header(
                message, toast_type=toast_type, refresh_filled_tree=refresh_filled_tree
            ),
            "HX-Reswap": "none",
        },
    )


# ---------- serialization ----------


def _serialize_step(step: RequestChainStep) -> dict[str, Any]:
    return {
        "id": str(step.id),
        "name": step.name_snapshot,
        "method": step.http_method_snapshot or "",
        "url": step.url_snapshot or "",
        "headers": step.headers_snapshot or [],
        "format": step.format or "json",
        "body": step.body or "",
        "mock_response": step.mock_response or "",
    }


def _serialize_chain(chain: RequestChain) -> dict[str, Any]:
    return {
        "id": str(chain.id),
        "name": chain.name,
        "steps": [_serialize_step(s) for s in chain.steps],
    }


def _step_dependencies(steps: list[dict[str, Any]]) -> dict[int, list[int]]:
    """Map each step position (1-based) to the prior step numbers it references
    via ``{{ $N.path }}`` tokens in its body — drives the «зависит от шага N»
    badges on the standalone chain page."""

    pattern = re.compile(r"\{\{\s*\$(\d+)\.[^}\s]+\s*\}\}")
    deps: dict[int, list[int]] = {}
    for idx, step in enumerate(steps, start=1):
        found: list[int] = []
        for m in pattern.finditer(step.get("body") or ""):
            n = int(m.group(1))
            # Only prior steps are real dependencies; a self/forward reference
            # ($N with N >= this step) can't resolve, so don't badge it.
            if 1 <= n < idx and n not in found:
                found.append(n)
        deps[idx] = sorted(found)
    return deps


# ---------- tree / panel rendering ----------


async def _tree_response(
    request: Request,
    templates: Jinja2Templates,
    session: AsyncSession,
    *,
    search: str = "",
    headers: dict[str, str] | None = None,
    visible_group_ids: set[uuid.UUID] | None = None,
) -> Response:
    context = await FilledTemplateService(session).build_tree(
        search=search, visible_group_ids=visible_group_ids
    )
    return templates.TemplateResponse(
        request, "partials/filled_tree.html", context, headers=headers
    )


async def _panel_context(
    session: AsyncSession,
    chain_id: uuid.UUID,
    *,
    visible_group_ids: set[uuid.UUID] | None = None,
) -> dict[str, Any]:
    chain = await RequestChainService(session).get(
        chain_id, visible_group_ids=visible_group_ids
    )
    data = _serialize_chain(chain)
    # Filled templates the «Добавить шаг» picker can choose from (id/name/method).
    filled = await FilledTemplateService(session).list_all(visible_group_ids=visible_group_ids)
    available = [
        {
            "id": str(f.id),
            "name": f.name,
            "method": f.http_method_snapshot or "",
            "project_name": f.project_name_snapshot or "",
            "project_color": f.project_color_snapshot or "",
        }
        for f in filled
    ]
    return {
        "chain": chain,
        "chain_data": data,
        "available": available,
        "dependencies": _step_dependencies(data["steps"]),
        "execute_url": "/templater/send-htmx/execute",
    }


async def _panel_response(
    request: Request,
    templates: Jinja2Templates,
    session: AsyncSession,
    chain_id: uuid.UUID,
    *,
    standalone: bool = False,
    headers: dict[str, str] | None = None,
    visible_group_ids: set[uuid.UUID] | None = None,
) -> Response:
    context = await _panel_context(session, chain_id, visible_group_ids=visible_group_ids)
    return templates.TemplateResponse(
        request,
        "partials/chain_panel.html",
        {"standalone": standalone, **context},
        headers=headers,
    )


# ---------- chain CRUD (return the tree) ----------


@router.post("/filled-templates-htmx/chains")
async def htmx_create_chain(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    form = await request.form()
    parent = parse_json_path(form_str(form, "parent"))
    name = form_str(form, "name")
    search = form_str(form, "search")
    try:
        await RequestChainService(session).create_chain(parent, name)
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return await _tree_response(
            request, templates, session, search=search,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
            visible_group_ids=group_ids,
        )
    return await _tree_response(
        request, templates, session, search=search,
        headers={"HX-Trigger": toast_header(f"Цепочка «{name.strip()}» создана")},
        visible_group_ids=group_ids,
    )


@router.post("/filled-templates-htmx/chains/{chain_id}/rename")
async def htmx_rename_chain(
    chain_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    form = await request.form()
    name = form_str(form, "name")
    search = form_str(form, "search")
    try:
        await RequestChainService(session).rename_chain(
            chain_id, name, visible_group_ids=group_ids
        )
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return await _tree_response(
            request, templates, session, search=search,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
            visible_group_ids=group_ids,
        )
    return await _tree_response(
        request, templates, session, search=search,
        headers={"HX-Trigger": toast_header("Цепочка переименована")},
        visible_group_ids=group_ids,
    )


@router.delete("/filled-templates-htmx/chains/{chain_id}")
async def htmx_delete_chain(
    chain_id: uuid.UUID,
    request: Request,
    panel: bool = False,
    redirect: bool = False,
    search: str = "",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    try:
        await RequestChainService(session).delete_chain(chain_id, visible_group_ids=group_ids)
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        # From the standalone page (redirect) or the inline panel (panel) there
        # is no tree to swap into — show a toast and keep the current view.
        if redirect or panel:
            return _toast_only(exc.message, "error")
        return await _tree_response(
            request, templates, session, search=search,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
            visible_group_ids=group_ids,
        )
    if redirect:
        # Deleted from the standalone page — send the user back to the workspace.
        return Response(
            status_code=200,
            headers={
                "HX-Redirect": "/templater/filled-templates",
                "HX-Trigger": toast_header("Цепочка удалена"),
            },
        )
    if panel:
        # Deleted from the workspace panel: swap in the empty-state and let the
        # refresh-filled-tree trigger reload the tree.
        return templates.TemplateResponse(
            request,
            "partials/filled_panel_empty.html",
            {},
            headers={"HX-Trigger": toast_header("Цепочка удалена", refresh_filled_tree=True)},
        )
    return await _tree_response(
        request, templates, session, search=search,
        headers={"HX-Trigger": toast_header("Цепочка удалена")},
        visible_group_ids=group_ids,
    )


# ---------- chain panel + standalone page ----------


@router.get("/filled-templates-htmx/chains/{chain_id}/panel")
async def htmx_chain_panel(
    chain_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    try:
        return await _panel_response(
            request, templates, session, chain_id, visible_group_ids=group_ids
        )
    except DomainError as exc:
        # The chain was deleted/hidden between the tree render and the click:
        # swap the empty-state into the panel and refresh the tree to drop it,
        # instead of letting the global handler swap raw JSON 404 into the panel.
        return templates.TemplateResponse(
            request,
            "partials/filled_panel_empty.html",
            {},
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error", refresh_filled_tree=True)},
        )


@router.get("/chains/{chain_id}")
async def page_chain(
    chain_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    try:
        context = await _panel_context(session, chain_id, visible_group_ids=group_ids)
    except DomainError:
        # Full-page nav to a missing/hidden chain — send the user to the
        # workspace rather than rendering a raw JSON 404 in the browser.
        return RedirectResponse("/templater/filled-templates", status_code=303)
    return templates.TemplateResponse(
        request,
        "chains/view.html",
        {"active": "templates", "standalone": True, **context},
    )


# ---------- steps (return the chain panel) ----------


@router.post("/filled-templates-htmx/chains/{chain_id}/steps")
async def htmx_add_step(
    chain_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    form = await request.form()
    # The picker rides on both the inline workspace panel and the standalone
    # page; ``standalone`` keeps the re-rendered partial's header consistent.
    standalone = form_str(form, "standalone") == "1"
    try:
        filled_id = uuid.UUID(form_str(form, "filled_id"))
    except ValueError:
        return _toast_only("Некорректный шаблон", "error")
    try:
        await RequestChainService(session).add_step(
            chain_id, filled_id, visible_group_ids=group_ids
        )
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        # Toast and keep the current panel — re-rendering it here would re-fetch
        # the chain and could raise again (e.g. the chain became invisible),
        # swapping raw JSON into the panel.
        return _toast_only(exc.message, "error")
    return await _panel_response(
        request, templates, session, chain_id, standalone=standalone,
        headers={"HX-Trigger": toast_header("Шаг добавлен", refresh_filled_tree=True)},
        visible_group_ids=group_ids,
    )


@router.delete("/filled-templates-htmx/chains/{chain_id}/steps/{step_id}")
async def htmx_remove_step(
    chain_id: uuid.UUID,
    step_id: uuid.UUID,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    # Driven by the Alpine panel (optimistic local splice), so this just persists
    # and returns 204; the client triggers the tree refresh for the step count.
    try:
        await RequestChainService(session).remove_step(
            chain_id, step_id, visible_group_ids=group_ids
        )
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return JSONResponse(status_code=400, content={"message": exc.message})
    return Response(status_code=204)


@router.post("/filled-templates-htmx/chains/{chain_id}/steps/reorder")
async def htmx_reorder_steps(
    chain_id: uuid.UUID,
    request: Request,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    form = await request.form()
    order = parse_uuid_list(form_str(form, "order"))
    try:
        await RequestChainService(session).reorder_steps(
            chain_id, order, visible_group_ids=group_ids
        )
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return JSONResponse(status_code=400, content={"message": exc.message})
    # Client already reflects the new order optimistically — no body to swap.
    return Response(status_code=204)


@router.post("/filled-templates-htmx/chains/{chain_id}/steps/{step_id}")
async def htmx_update_step(
    chain_id: uuid.UUID,
    step_id: uuid.UUID,
    request: Request,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    form = await request.form()
    body = form_str(form, "body") if "body" in form else None
    mock_response = form_str(form, "mock_response") if "mock_response" in form else None
    try:
        await RequestChainService(session).update_step(
            chain_id, step_id, body=body, mock_response=mock_response,
            visible_group_ids=group_ids,
        )
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return JSONResponse(status_code=400, content={"message": exc.message})
    return Response(status_code=204)


# ---------- stub «send» seam (NO real network request) ----------


async def _simulate_latency(latency_ms: int) -> None:
    """Mock network latency for the stub send. Module-local so tests can patch
    it without touching the global ``asyncio.sleep``; the real send tool will
    replace this with an actual call."""

    await asyncio.sleep(latency_ms / 1000)


@router.post("/send-htmx/execute")
async def htmx_execute(request: Request) -> JSONResponse:
    """Stub «send» seam — DOES NOT make any network request.

    The real sending tool is not implemented yet and will be exposed over an API
    later. For now this echoes the step's editable example response back as the
    response body so the UI can demonstrate the flow. When the real tool lands,
    swap the body of this handler to call it (the request envelope —
    method/url/headers/body/format — is already provided by the client).
    """

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_json", "message": "Тело запроса не является корректным JSON"},
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_json", "message": "Тело запроса должно быть JSON-объектом"},
        )

    mock_response = payload.get("mock_response", "")
    latency_ms = random.randint(35, 220)
    await _simulate_latency(latency_ms)
    return JSONResponse(
        {
            "status": 200,
            "status_text": "OK",
            "latency_ms": latency_ms,
            "headers": {"Content-Type": "application/json; charset=utf-8", "X-Mock-Send": "true"},
            "body": mock_response,
        }
    )
