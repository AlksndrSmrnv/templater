from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from app.routes.deps import TemplatesDep

router = APIRouter()


@router.get("/send")
async def page_send(request: Request, templates: Jinja2Templates = TemplatesDep):
    return templates.TemplateResponse(request, "send.html", {"active": "send"})
