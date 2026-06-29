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
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    SEND_SOURCE_CHAIN_STEP,
    SEND_SOURCE_FILLED,
    FilledTemplate,
    MessageSend,
    RequestChain,
    RequestChainStep,
)
from app.repositories.entity import ClientRepository
from app.repositories.filled_template import FilledTemplateRepository
from app.repositories.message_send import LastSends, MessageSendRepository
from app.routes.client_switch_utils import role_ids_from_form
from app.routes.deps import SessionDep, TemplatesDep, UnlockedGroupsDep
from app.routes.entities_htmx import entity_label
from app.routes.htmx_utils import form_str, parse_json_path, parse_uuid_list, toast_header
from app.routes.uow import commit_or_409
from app.services.filled_templates import (
    _ROLE_COLUMNS,
    ROLE_TITLES,
    FilledTemplateService,
)
from app.services.request_chain import RequestChainService
from app.services.template_render import render_chain_step_html
from app.utils.errors import DomainError
from app.utils.status_code import extract_status_code

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
    step: RequestChainStep,
    message_template_id: uuid.UUID | None = None,
    last: LastSends | None = None,
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
        # Roles bound on this step — drives the per-step «Заменить» buttons inside
        # the Alpine step card.
        "roles": _step_roles(step),
        # Last successful / failed send of this step as ISO-8601 ("" when none) —
        # the client formats it (window.formatSendTs) so the badge reads in the
        # user's zone and matches a locally updated value after a send.
        "last_success_at": (
            last.success_at.isoformat() if last and last.success_at else ""
        ),
        "last_error_at": (
            last.error_at.isoformat() if last and last.error_at else ""
        ),
    }


def _step_roles(step: RequestChainStep) -> list[dict[str, Any]]:
    """``[{role, title, label, clientId, accountId, cardId}]`` for each role the
    step has populated (sender → receiver → accountOwner order)."""

    labels = step.role_labels_snapshot or {}
    out: list[dict[str, Any]] = []
    for role, (client_col, account_col, card_col) in _ROLE_COLUMNS.items():
        cid = getattr(step, client_col)
        if cid is None:
            continue
        out.append(
            {
                "role": role,
                "title": ROLE_TITLES[role],
                "label": labels.get(role) or "—",
                "clientId": str(cid),
                "accountId": str(getattr(step, account_col) or ""),
                "cardId": str(getattr(step, card_col) or ""),
            }
        )
    return out


def _serialize_chain(
    chain: RequestChain,
    tpl_by_filled: dict[uuid.UUID, uuid.UUID | None] | None = None,
    last_by_step: dict[uuid.UUID, LastSends] | None = None,
) -> dict[str, Any]:
    tpl_by_filled = tpl_by_filled or {}
    last_by_step = last_by_step or {}
    return {
        "id": str(chain.id),
        "name": chain.name,
        "steps": [
            _serialize_step(
                s,
                tpl_by_filled.get(s.filled_template_id) if s.filled_template_id else None,
                last_by_step.get(s.id),
            )
            for s in chain.steps
        ],
    }


async def _manage_context(
    session: AsyncSession,
    chain: RequestChain,
    tpl_by_filled: dict[uuid.UUID, uuid.UUID | None],
) -> dict[str, Any]:
    """Build the «Клиенты всей цепочки» top controls: the roles present across the
    chain (sender → receiver → accountOwner), each with a label that shows the
    common client when every step agrees (else «разные»), plus the (account/card)
    ids to preselect in that uniform case, and a picker template id.

    The fill endpoints use ``manage_template_id`` only for role validation
    (``_check_fill_role``) — the client/account/card lists are global, NOT
    filtered by template — so any reachable step's template works even when the
    chain spans several; each step is re-rendered against its own source template.
    """

    any_template_id = ""
    for step in chain.steps:
        tpl_id = tpl_by_filled.get(step.filled_template_id) if step.filled_template_id else None
        if tpl_id:
            any_template_id = str(tpl_id)
            break

    chain_roles: list[dict[str, Any]] = []
    for role, (client_col, account_col, card_col) in _ROLE_COLUMNS.items():
        # Steps that have this role populated.
        present = [s for s in chain.steps if getattr(s, client_col) is not None]
        if not present:
            continue
        triples = {
            (
                getattr(s, client_col),
                getattr(s, account_col),
                getattr(s, card_col),
            )
            for s in present
        }
        uniform = len(triples) == 1
        client_id, account_id, card_id = next(iter(triples)) if uniform else (None, None, None)
        label = ""
        if uniform and client_id is not None:
            row = await ClientRepository(session).get(client_id)
            label = entity_label("client", row) if row is not None else ""
        chain_roles.append(
            {
                "role": role,
                "title": ROLE_TITLES[role],
                "label": label,  # "" → render «разные» / «—» in the template
                "uniform": uniform,
                "current": {
                    "clientId": str(client_id or ""),
                    "accountId": str(account_id or ""),
                    "cardId": str(card_id or ""),
                },
            }
        )
    return {"chain_roles": chain_roles, "manage_template_id": any_template_id}


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
    # Latest success/failure per step, for the «последняя отправка» badges.
    last_by_step = await MessageSendRepository(session).last_for_chain_steps(
        [s.id for s in chain.steps]
    )
    data = _serialize_chain(chain, tpl_by_filled, last_by_step)
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
        # From the inline panel (panel) there is no tree to swap into — show a
        # toast and keep the current view.
        if panel:
            return _toast_only(exc.message, "error")
        return await _tree_response(
            request, templates, session, search=search,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
            visible_group_ids=group_ids,
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


# ---------- chain panel ----------


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
    # ``standalone`` keeps the re-rendered partial's header consistent (the
    # workspace panel always renders its own header).
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


@router.post("/filled-templates-htmx/chains/{chain_id}/replace-role")
async def htmx_replace_role(
    chain_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    """Replace one role's client across every step of the chain that has it."""

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
        changed, regenerated = await RequestChainService(session).replace_role_everywhere(
            chain_id, role, new_ids, visible_group_ids=group_ids
        )
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return _toast_only(exc.message, "error")
    if not changed:
        return _toast_only("Эта роль не используется ни в одном шаге", "error")
    message = f"{ROLE_TITLES[role]} заменён в {len(changed)} шаг(ах)"
    toast_type = "success"
    # Some steps lost their source template — their role updated but body not
    # regenerated; say so rather than implying every body was rebuilt.
    if regenerated < len(changed):
        message += f" (тело перегенерировано в {regenerated})"
        toast_type = "warning"
    return await _panel_response(
        request, templates, session, chain_id, standalone=standalone,
        headers={"HX-Trigger": toast_header(
            message, toast_type=toast_type, refresh_filled_tree=True
        )},
        visible_group_ids=group_ids,
    )


# ---------- stub «send» seam (NO real network request) ----------


async def _simulate_latency(latency_ms: int) -> None:
    """Mock network latency for the stub send. Module-local so tests can patch
    it without touching the global ``asyncio.sleep``; the real send tool will
    replace this with an actual call."""

    await asyncio.sleep(latency_ms / 1000)


def _opt_uuid(value: Any) -> uuid.UUID | None:
    """Parse an optional UUID from the send payload; ``None`` on missing/bad."""

    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


async def _record_send(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    ok: bool,
    http_status: int | None,
    status_code: int | None,
    response_headers: dict[str, Any],
    response_body: str,
    latency_ms: int | None,
    error_message: str = "",
) -> None:
    """Persist one send to the history (``message_sends``).

    The request envelope and (mock) response are snapshotted so the row survives
    its source being deleted. A source id whose row no longer exists (e.g. a step
    removed between panel render and send) is dropped to NULL rather than failing
    the send with an FK violation.
    """

    filled_id = _opt_uuid(payload.get("filled_template_id"))
    chain_id = _opt_uuid(payload.get("chain_id"))
    step_id = _opt_uuid(payload.get("chain_step_id"))

    # Resolve source_kind from the client's intent *before* dropping stale ids —
    # a chain-step send whose step was just deleted is still a chain-step send,
    # not re-labelled «filled» because its FK survived. The id fallback only
    # applies when the client sent no/invalid source_kind.
    source_kind = str(payload.get("source_kind") or "")
    if source_kind not in (SEND_SOURCE_FILLED, SEND_SOURCE_CHAIN_STEP):
        source_kind = SEND_SOURCE_CHAIN_STEP if step_id is not None else SEND_SOURCE_FILLED

    # Drop ids whose row no longer exists so the INSERT's FKs hold (snapshot
    # columns keep the row meaningful regardless).
    if filled_id is not None and await session.get(FilledTemplate, filled_id) is None:
        filled_id = None
    if step_id is not None and await session.get(RequestChainStep, step_id) is None:
        step_id = None
    if chain_id is not None and await session.get(RequestChain, chain_id) is None:
        chain_id = None

    headers = payload.get("headers")
    record = MessageSend(
        source_kind=source_kind,
        filled_template_id=filled_id,
        chain_id=chain_id,
        chain_step_id=step_id,
        name_snapshot=str(payload.get("name") or "")[:255],
        http_method=str(payload.get("method") or "")[:16],
        url=str(payload.get("url") or ""),
        request_headers=headers if isinstance(headers, list) else [],
        request_body=str(payload.get("body") or ""),
        ok=ok,
        http_status=http_status,
        status_code=status_code,
        response_headers=response_headers,
        response_body=response_body if isinstance(response_body, str) else "",
        latency_ms=latency_ms,
        error_message=error_message,
    )
    await MessageSendRepository(session).add(record)
    await commit_or_409(session, message="Не удалось сохранить историю отправки")


@router.post("/send-htmx/execute")
async def htmx_execute(
    request: Request,
    session: AsyncSession = SessionDep,
) -> JSONResponse:
    """Stub «send» seam — DOES NOT make any network request, but records the send.

    The real sending tool is not implemented yet and will be exposed over an API
    later. For now this echoes the step's editable example response back as the
    response body so the UI can demonstrate the flow. Every send is persisted to
    the history (``message_sends``) — what was sent, where, and the (mock)
    response — driving the history drawer and the per-button «last send» badges.
    When the real tool lands, swap the mock block below for the actual call; the
    recording around it stays the same.
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
    if not isinstance(mock_response, str):
        mock_response = ""
    latency_ms = random.randint(35, 220)
    await _simulate_latency(latency_ms)

    http_status = 200
    response_headers = {"Content-Type": "application/json; charset=utf-8", "X-Mock-Send": "true"}
    status_code = extract_status_code(mock_response)
    # A non-zero statusCode in the response body is a logical failure (shown red
    # in the UI); absent/zero counts as success. Transport never fails for the
    # mock — the real send will set ok=False on network/HTTP errors instead.
    ok = status_code is None or status_code == 0
    # The mock has no transport error to describe, so error_message stays empty
    # even when ok=False (the reason is the non-zero statusCode in the body).
    # TODO: the real sender should pass the network/HTTP error text here.
    await _record_send(
        session,
        payload,
        ok=ok,
        http_status=http_status,
        status_code=status_code,
        response_headers=response_headers,
        response_body=mock_response,
        latency_ms=latency_ms,
    )

    return JSONResponse(
        {
            "status": http_status,
            "status_text": "OK",
            "latency_ms": latency_ms,
            "headers": response_headers,
            "body": mock_response,
        }
    )
