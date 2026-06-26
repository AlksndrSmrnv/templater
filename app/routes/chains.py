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
from app.repositories.entity import ClientRepository
from app.repositories.filled_template import FilledTemplateRepository
from app.routes.deps import SessionDep, TemplatesDep, UnlockedGroupsDep
from app.routes.entities_htmx import entity_label
from app.routes.htmx_utils import form_str, parse_json_path, parse_uuid_list, toast_header
from app.routes.role_switch_utils import role_ids_from_form
from app.routes.uow import commit_or_409
from app.services.filled_templates import (
    _ROLE_COLUMNS,
    ROLE_TITLES,
    FilledTemplateService,
)
from app.services.request_chain import RequestChainService
from app.services.template_render import render_chain_step_html
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


def _serialize_step(
    step: RequestChainStep, message_template_id: uuid.UUID | None = None
) -> dict[str, Any]:
    body = step.body or ""
    fmt = step.format or "json"
    changed = step.changed_locations or []
    return {
        "id": str(step.id),
        "name": step.name_snapshot,
        "method": step.http_method_snapshot or "",
        "url": step.url_snapshot or "",
        "headers": step.headers_snapshot or [],
        "format": fmt,
        "body": body,
        # Coloured, clickable markup of the body (blue dynamic / green filled /
        # purple reference / white literal) — re-rendered server-side after each
        # bind/unbind so the Alpine panel can update one step in place.
        "body_html": render_chain_step_html(fmt, body, changed),
        "changed_locations": changed,
        "mock_response": step.mock_response or "",
        # Source template of this step's body (via its filled template), so the
        # «Заменить клиента» picker can target its fill endpoints. Empty when the
        # filled template / source template was deleted (switch then disabled).
        "message_template_id": str(message_template_id) if message_template_id else "",
    }


def _serialize_chain(
    chain: RequestChain, tpl_by_filled: dict[uuid.UUID, uuid.UUID | None] | None = None
) -> dict[str, Any]:
    tpl_by_filled = tpl_by_filled or {}
    return {
        "id": str(chain.id),
        "name": chain.name,
        "steps": [
            _serialize_step(
                s,
                tpl_by_filled.get(s.filled_template_id) if s.filled_template_id else None,
            )
            for s in chain.steps
        ],
    }


async def _manage_context(
    session: AsyncSession,
    chain: RequestChain,
    tpl_by_filled: dict[uuid.UUID, uuid.UUID | None],
) -> dict[str, Any]:
    """Build the «Клиенты цепочки» management data: per-step role rows (with
    current ids + a picker template id) and the distinct clients used across the
    chain (for the chain-wide replace popovers)."""

    used: dict[uuid.UUID, dict[str, Any]] = {}
    # A template id for the chain-wide replace picker. The fill endpoints use it
    # only for role validation (``_check_fill_role``) — the client/account/card
    # lists are global, NOT filtered by template — so any reachable step's
    # template works even when the chain spans several templates; each step is
    # re-rendered against its own source template in ``_rerender_step``.
    any_template_id = ""
    manage_steps: list[dict[str, Any]] = []
    for idx, step in enumerate(chain.steps, start=1):
        tpl_id = tpl_by_filled.get(step.filled_template_id) if step.filled_template_id else None
        tpl_str = str(tpl_id) if tpl_id else ""
        if tpl_str and not any_template_id:
            any_template_id = tpl_str
        labels = step.role_labels_snapshot or {}
        roles: list[dict[str, Any]] = []
        for role, (client_col, account_col, card_col) in _ROLE_COLUMNS.items():
            cid = getattr(step, client_col)
            if cid is None:
                continue
            roles.append(
                {
                    "role": role,
                    "title": ROLE_TITLES[role],
                    "label": labels.get(role) or "—",
                    "client_id": str(cid),
                    "account_id": str(getattr(step, account_col) or ""),
                    "card_id": str(getattr(step, card_col) or ""),
                }
            )
            used.setdefault(cid, {"id": str(cid), "label": labels.get(role) or str(cid)})
        manage_steps.append(
            {
                "id": str(step.id),
                "num": idx,
                "name": step.name_snapshot,
                "template_id": tpl_str,
                "roles": roles,
            }
        )
    # Prefer a clean client display label over the combined role label.
    if used:
        rows = await ClientRepository(session).get_many(list(used.keys()))
        for row in rows:
            if row.id in used:
                used[row.id]["label"] = entity_label("client", row)
    return {
        "manage_steps": manage_steps,
        "used_clients": list(used.values()),
        "manage_template_id": any_template_id,
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
    # Map each step's source filled template to its message template, so the
    # «Заменить клиента» picker can target the right fill endpoints.
    filled_ids = [s.filled_template_id for s in chain.steps if s.filled_template_id]
    tpl_by_filled: dict[uuid.UUID, uuid.UUID | None] = {}
    if filled_ids:
        for row in await FilledTemplateRepository(session).get_many(filled_ids):
            tpl_by_filled[row.id] = row.message_template_id
    data = _serialize_chain(chain, tpl_by_filled)
    manage = await _manage_context(session, chain, tpl_by_filled)
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
        **manage,
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


def _step_body_json(step: RequestChainStep) -> JSONResponse:
    """The refreshed body + coloured markup for one step, for in-place client
    updates after a bind/unbind (no full-panel re-render, so other steps' sent
    responses survive)."""

    body = step.body or ""
    fmt = step.format or "json"
    return JSONResponse(
        {"body": body, "body_html": render_chain_step_html(fmt, body, step.changed_locations or [])}
    )


@router.post("/filled-templates-htmx/chains/{chain_id}/steps/{step_id}/bind")
async def htmx_bind_field(
    chain_id: uuid.UUID,
    step_id: uuid.UUID,
    request: Request,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    """Bind a body field (by ``location``) to ``{{ $ref_step.ref_path }}`` —
    a field of an earlier step's response. Returns the refreshed step body."""

    form = await request.form()
    location = form_str(form, "location")
    ref_path = form_str(form, "ref_path")
    try:
        ref_step = int(form_str(form, "ref_step"))
    except ValueError:
        return JSONResponse(status_code=400, content={"message": "Некорректный шаг-источник"})
    try:
        step = await RequestChainService(session).bind_field(
            chain_id, step_id, location=location, ref_step=ref_step,
            ref_path=ref_path, visible_group_ids=group_ids,
        )
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return JSONResponse(status_code=400, content={"message": exc.message})
    return _step_body_json(step)


@router.post("/filled-templates-htmx/chains/{chain_id}/steps/{step_id}/unbind")
async def htmx_unbind_field(
    chain_id: uuid.UUID,
    step_id: uuid.UUID,
    request: Request,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    """Reset a previously bound body field back to its original value."""

    form = await request.form()
    location = form_str(form, "location")
    try:
        step = await RequestChainService(session).unbind_field(
            chain_id, step_id, location=location, visible_group_ids=group_ids,
        )
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return JSONResponse(status_code=400, content={"message": exc.message})
    return _step_body_json(step)


# ---------- client switching (return the chain panel) ----------


@router.post("/filled-templates-htmx/chains/{chain_id}/steps/{step_id}/switch-role")
async def htmx_switch_step_role(
    chain_id: uuid.UUID,
    step_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    form = await request.form()
    standalone = form_str(form, "standalone") == "1"
    role = form_str(form, "role")
    if role not in ROLE_TITLES:
        return _toast_only("Неизвестная роль", "error")
    try:
        new_ids = role_ids_from_form(form)
    except ValueError:
        return _toast_only("Некорректный выбор", "error")
    try:
        _, regenerated = await RequestChainService(session).switch_step_client(
            chain_id, step_id, role, new_ids, visible_group_ids=group_ids
        )
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return _toast_only(exc.message, "error")
    message = (
        "Клиент заменён"
        if regenerated
        else "Исходный шаблон удалён — тело не перегенерировано, обновлены роли и имя"
    )
    return await _panel_response(
        request, templates, session, chain_id, standalone=standalone,
        headers={"HX-Trigger": toast_header(
            message, toast_type="success" if regenerated else "warning",
            refresh_filled_tree=True,
        )},
        visible_group_ids=group_ids,
    )


@router.post("/filled-templates-htmx/chains/{chain_id}/replace-client")
async def htmx_replace_client(
    chain_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    form = await request.form()
    standalone = form_str(form, "standalone") == "1"
    try:
        old_client_id = uuid.UUID(form_str(form, "old_client_id"))
        new_ids = role_ids_from_form(form)
    except ValueError:
        return _toast_only("Некорректный выбор", "error")
    try:
        changed = await RequestChainService(session).replace_client_everywhere(
            chain_id, old_client_id, new_ids, visible_group_ids=group_ids
        )
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return _toast_only(exc.message, "error")
    if not changed:
        return _toast_only("Этот клиент не используется в цепочке", "error")
    return await _panel_response(
        request, templates, session, chain_id, standalone=standalone,
        headers={"HX-Trigger": toast_header(
            f"Клиент заменён в {len(changed)} шаг(ах)", refresh_filled_tree=True
        )},
        visible_group_ids=group_ids,
    )


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
