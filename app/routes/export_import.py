from __future__ import annotations

import json

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.settings import SettingsRepository
from app.routes.deps import SessionDep, TemplatesDep
from app.schemas.exchange import ExportRequest
from app.services.export_import import ExportImportService
from app.utils.errors import ValidationFailed

router = APIRouter()

VALID_POLICIES = {"skip", "overwrite", "fail"}


@router.get("/import")
async def page_import(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
):
    saved = await SettingsRepository(session).get("import_policy", "skip")
    default_policy = saved if saved in VALID_POLICIES else "skip"
    return templates.TemplateResponse(
        request,
        "import.html",
        {"active": "data", "default_policy": default_policy},
    )


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
    policy: str | None = Form(None),
    session: AsyncSession = SessionDep,
):
    try:
        raw = await file.read()
        package = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValidationFailed(f"Не удалось прочитать файл: {exc}")

    if policy is None or policy not in VALID_POLICIES:
        # Fall back to the saved default from settings (set via /settings UI).
        saved = await SettingsRepository(session).get("import_policy", "skip")
        policy = saved if saved in VALID_POLICIES else "skip"

    summary = await ExportImportService(session).import_package(package, policy=policy)
    return JSONResponse(content=summary.model_dump())
