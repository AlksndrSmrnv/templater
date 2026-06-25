"""Routes for the «Отправка сообщений» page — a Postman-like chain runner.

The page lets the user assemble an ordered chain of requests out of existing
«Заполненные шаблоны» (each already carries a URL, headers and a body filled
with test data), «send» each step and reference fields from earlier responses
in later requests. The chain itself lives entirely in the browser
(Alpine.js + ``localStorage``); the server only

- ``GET /send`` — renders the page and seeds the picker with a lightweight list
  of filled templates;
- ``GET /send-htmx/filled/{id}`` — returns one filled template's request
  snapshot (method, url, headers, body) as JSON when a step is added;
- ``POST /send-htmx/execute`` — a *stub* «send» seam (see its docstring).
"""

from __future__ import annotations

import asyncio
import json
import random
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.routes.deps import SessionDep, TemplatesDep, UnlockedGroupsDep
from app.services.filled_templates import FilledTemplateService

router = APIRouter()

# The picker loads its whole list up front (it filters client-side), so allow a
# generous cap and flag truncation when it is hit instead of silently dropping
# templates past the default list limit.
PICKER_LIMIT = 1000


@router.get("/send")
async def page_send(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> Response:
    items = await FilledTemplateService(session).list_all(
        limit=PICKER_LIMIT, visible_group_ids=group_ids
    )
    filled_templates = [
        {
            "id": str(item.id),
            "name": item.name,
            "method": item.http_method_snapshot or "",
            "project_name": item.project_name_snapshot or "",
            "project_color": item.project_color_snapshot or "",
        }
        for item in items
    ]
    return templates.TemplateResponse(
        request,
        "send.html",
        {
            "active": "send",
            "filled_templates": filled_templates,
            "picker_truncated": len(items) >= PICKER_LIMIT,
        },
    )


@router.get("/send-htmx/filled/{filled_id}")
async def htmx_filled_snapshot(
    filled_id: uuid.UUID,
    session: AsyncSession = SessionDep,
    group_ids: set[uuid.UUID] = UnlockedGroupsDep,
) -> JSONResponse:
    """Request snapshot for one chain step (raises 404 via the DomainError handler)."""

    item = await FilledTemplateService(session).get(
        filled_id, visible_group_ids=group_ids
    )
    return JSONResponse(
        {
            "id": str(item.id),
            "name": item.name,
            "method": item.http_method_snapshot or "",
            "url": item.url_snapshot or "",
            "headers": item.headers_snapshot or [],
            "format": item.format,
            "body": item.filled_content or "",
            "project_name": item.project_name_snapshot or "",
            "project_color": item.project_color_snapshot or "",
        }
    )


@router.post("/send-htmx/execute")
async def htmx_execute(request: Request) -> JSONResponse:
    """Stub «send» seam — DOES NOT make any network request.

    The real sending tool is not implemented yet and will be exposed over an API
    later. For now this endpoint simply echoes the step's editable mock response
    back as the response body so the UI can demonstrate the flow. When the real
    tool lands, swap the body of this handler to call it (the request envelope —
    method/url/headers/body/format — is already provided by the client).
    """

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_json",
                "message": "Тело запроса должно быть корректным JSON",
            },
        )
    if not isinstance(payload, dict):
        payload = {}

    mock_response = payload.get("mock_response", "")
    # Simulate a little network latency so the reported value reflects a real wait.
    latency_ms = random.randint(35, 220)
    await asyncio.sleep(latency_ms / 1000)
    return JSONResponse(
        {
            "status": 200,
            "status_text": "OK",
            "latency_ms": latency_ms,
            "headers": {
                "Content-Type": "application/json; charset=utf-8",
                "X-Mock-Send": "true",
            },
            "body": mock_response,
        }
    )
