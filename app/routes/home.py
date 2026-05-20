from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from app.routes.deps import TemplatesDep

router = APIRouter()


@router.get("/")
async def home(request: Request, templates: Jinja2Templates = TemplatesDep) -> Response:
    return templates.TemplateResponse(request, "home.html", {"active": "home"})
