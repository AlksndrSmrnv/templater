"""Send-history drawer endpoints.

Each «Отправить» on a filled template / chain step is persisted to
``message_sends`` (see :mod:`app.routes.chains`). These endpoints render that
history as a table into the page-level drawer (``#send-history-content``) on the
«Заполненные шаблоны» workspace — scoped to the current object (this filled
template / this chain). Visibility mirrors the panels: a private object the
caller has not unlocked yields an empty drawer rather than leaking its sends.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageSend
from app.repositories.filled_template import FilledTemplateRepository
from app.repositories.message_send import DEFAULT_HISTORY_LIMIT, MessageSendRepository
from app.repositories.request_chain import RequestChainRepository
from app.routes.deps import SessionDep, TemplatesDep, UnlockedGroupsDep

router = APIRouter()


def _render(
    request: Request, templates: Jinja2Templates, *, title: str, sends: list[MessageSend]
) -> Response:
    return templates.TemplateResponse(
        request,
        "partials/send_history.html",
        {"title": title, "sends": sends},
    )


@router.get("/send-history-htmx/filled/{filled_id}")
async def htmx_history_filled(
    filled_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    filled = await FilledTemplateRepository(session).get(
        filled_id, visible_group_ids=group_ids
    )
    if filled is None:
        return _render(request, templates, title="История отправок", sends=[])
    sends = await MessageSendRepository(session).list_for_filled(filled_id)
    return _render(request, templates, title=f"История отправок · {filled.name}", sends=sends)


@router.get("/send-history-htmx/chain/{chain_id}")
async def htmx_history_chain(
    chain_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    chain = await RequestChainRepository(session).get(
        chain_id, visible_group_ids=group_ids
    )
    if chain is None:
        return _render(request, templates, title="История отправок", sends=[])
    sends = await MessageSendRepository(session).list_for_chain(chain_id)
    return _render(request, templates, title=f"История отправок · {chain.name}", sends=sends)


async def _history_context(
    session: AsyncSession, *, q: str, group_ids: set[uuid.UUID]
) -> dict[str, Any]:
    sends = await MessageSendRepository(session).search(query=q, visible_group_ids=group_ids)
    # The search caps at DEFAULT_HISTORY_LIMIT (no paging UI); flag the cap so the
    # table can tell the user the newest N are shown and there may be more.
    return {
        "q": q,
        "sends": sends,
        "list_limit": DEFAULT_HISTORY_LIMIT,
        "truncated": len(sends) >= DEFAULT_HISTORY_LIMIT,
    }


@router.get("/operations-history")
async def page_operations_history(
    request: Request,
    q: str = "",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    """Global «История операций» page — search across every send's history."""

    context = await _history_context(session, q=q, group_ids=group_ids)
    return templates.TemplateResponse(
        request,
        "operations_history/page.html",
        {"active": "operations-history", **context},
    )


@router.get("/operations-history-htmx/search")
async def htmx_operations_history_search(
    request: Request,
    q: str = "",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    """Results-table partial for the global history search box (live filter)."""

    context = await _history_context(session, q=q, group_ids=group_ids)
    return templates.TemplateResponse(
        request,
        "partials/operations_history_table.html",
        context,
    )
