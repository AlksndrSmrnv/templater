from __future__ import annotations

import json

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.routes.deps import SessionDep, TemplatesDep
from app.schemas.exchange import ExportRequest
from app.services.export_import import ExportImportService
from app.utils.errors import ValidationFailed

router = APIRouter()


@router.get("/import")
async def page_import(request: Request, templates: Jinja2Templates = TemplatesDep):
    return templates.TemplateResponse(request, "import.html", {"active": "data"})


@router.post("/api/export")
async def api_export(data: ExportRequest, session: AsyncSession = SessionDep):
    package = await ExportImportService(session).export(data)
    payload = json.dumps(package.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")

    def stream():
        yield payload

    return StreamingResponse(
        stream(),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="tm-export.json"'},
    )


@router.post("/api/import")
async def api_import(
    file: UploadFile = File(...),
    policy: str = Form("skip"),
    session: AsyncSession = SessionDep,
):
    try:
        raw = await file.read()
        package = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValidationFailed(f"Не удалось прочитать файл: {exc}")
    summary = await ExportImportService(session).import_package(package, policy=policy)
    return JSONResponse(content=summary.model_dump())
