from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.settings import SettingsRepository
from app.routes.deps import SessionDep, TemplatesDep
from app.services.export_import import ExportImportService
from app.utils.errors import DomainError

router = APIRouter()

VALID_POLICIES = {"skip", "overwrite", "fail"}


@router.get("/import")
async def page_import(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    saved = await SettingsRepository(session).get("import_policy", "skip")
    default_policy = saved if saved in VALID_POLICIES else "skip"
    return templates.TemplateResponse(
        request,
        "import.html",
        {"active": "data", "default_policy": default_policy},
    )


@router.post("/import-htmx")
async def htmx_import(
    request: Request,
    file: UploadFile = File(...),
    policy: str | None = Form(None),
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    try:
        raw = await file.read()
        package: Any = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "partials/import_result.html",
            {"ok": False, "message": f"Не удалось прочитать файл: {exc}", "details": []},
            status_code=422,
        )
    try:
        if policy is None or policy not in VALID_POLICIES:
            saved = await SettingsRepository(session).get("import_policy", "skip")
            policy = saved if saved in VALID_POLICIES else "skip"
        summary = await ExportImportService(session).import_package(package, policy=policy)
    except DomainError as exc:
        return templates.TemplateResponse(
            request,
            "partials/import_result.html",
            {"ok": False, "message": exc.message, "details": exc.details},
            status_code=exc.status_code,
        )
    return templates.TemplateResponse(
        request,
        "partials/import_result.html",
        {"ok": True, "summary": summary.model_dump()},
        headers={"HX-Trigger": json.dumps({"showToast": {"message": "Импорт завершён", "type": "success"}})},
    )
