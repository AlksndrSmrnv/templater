"""Unlock / lock access groups (no user accounts — the password is the key).

A successful unlock merges the group's id into the signed cookie and reissues
it (refreshing the 8h window); locking removes one group or clears the cookie
entirely. Administration of groups (create/rename/password) lives behind the
settings edit-mode gate in :mod:`app.routes.settings`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.routes.deps import SessionDep, TemplatesDep, UnlockedGroupsDep
from app.routes.htmx_utils import form_str, toast_header
from app.services.access_groups import AccessGroupService
from app.utils.access_groups import (
    COOKIE_NAME,
    COOKIE_PATH,
    TOKEN_TTL_SECONDS,
    issue_groups_token,
)

router = APIRouter()


@router.get("/groups-htmx/status")
async def htmx_status(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    """Navbar fragment: which groups are unlocked + the unlock affordance.

    Loaded lazily on every page (hx-trigger="load") so the control reflects the
    cookie without threading group state into every page context."""

    all_groups = await AccessGroupService(session).list_all()
    unlocked = [g for g in all_groups if g.id in group_ids]
    return templates.TemplateResponse(
        request,
        "partials/groups_navbar.html",
        {"unlocked": unlocked, "any_groups": bool(all_groups)},
    )


def _set_groups_cookie(response: Response, group_ids: set[uuid.UUID]) -> None:
    response.set_cookie(
        COOKIE_NAME,
        issue_groups_token(group_ids),
        max_age=TOKEN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path=COOKIE_PATH,
    )


@router.post("/groups-htmx/unlock")
async def htmx_unlock(
    request: Request,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    form = await request.form()
    group = await AccessGroupService(session).unlock(form_str(form, "password"))
    if group is None:
        # Status 200 (not 4xx) so htmx processes the HX-Trigger toast.
        return Response(
            status_code=200,
            headers={"HX-Trigger": toast_header("Неверный пароль", toast_type="error")},
        )
    updated = set(group_ids)
    updated.add(group.id)
    response = Response(status_code=204, headers={"HX-Refresh": "true"})
    _set_groups_cookie(response, updated)
    return response


@router.post("/groups-htmx/lock")
async def htmx_lock_all() -> Response:
    response = Response(status_code=204, headers={"HX-Refresh": "true"})
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return response


@router.post("/groups-htmx/lock/{group_id}")
async def htmx_lock_one(
    group_id: uuid.UUID,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    remaining = set(group_ids)
    remaining.discard(group_id)
    response = Response(status_code=204, headers={"HX-Refresh": "true"})
    if remaining:
        _set_groups_cookie(response, remaining)
    else:
        response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return response
