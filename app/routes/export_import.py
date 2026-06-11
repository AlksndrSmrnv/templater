from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.project import ProjectRepository
from app.repositories.settings import SettingsRepository
from app.routes.deps import SessionDep, TemplatesDep
from app.services.export_import import ExportImportService
from app.services.projects import DEFAULT_PROJECT_NAME
from app.utils.edit_mode import is_edit_mode
from app.utils.errors import DomainError

router = APIRouter()

VALID_POLICIES = {"skip", "overwrite", "fail"}


async def _missing_project_names(session: AsyncSession, package: Any) -> list[str]:
    """Project names the package's templates reference that don't exist yet.

    Mirrors the name resolution of ``ExportImportService.import_package``:
    blank/absent ``project_name`` falls back to «Без проекта», names are
    truncated to 255 chars. A non-empty result means the import would *create*
    projects — a settings mutation that must stay behind the edit-mode lock.
    """

    raw_templates = package.get("templates") if isinstance(package, dict) else None
    if not isinstance(raw_templates, list):
        return []
    names: set[str] = set()
    for raw in raw_templates:
        if not isinstance(raw, dict):
            continue
        name = raw.get("project_name")
        cleaned = name.strip() if isinstance(name, str) and name.strip() else DEFAULT_PROJECT_NAME
        names.add(cleaned[:255])
    repo = ProjectRepository(session)
    return [name for name in sorted(names) if await repo.get_by_name(name) is None]


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
    # attribute_schema entries create/overwrite attribute definitions — the
    # same mutation the settings page locks behind edit mode, so a data import
    # must not become a side door (data-only packages stay open to everyone).
    if isinstance(package, dict) and package.get("attribute_schema") and not is_edit_mode(request):
        return templates.TemplateResponse(
            request,
            "partials/import_result.html",
            {
                "ok": False,
                "message": (
                    "Файл содержит раздел attribute_schema — изменение атрибутов "
                    "доступно только в режиме редактирования настроек. Разблокируйте "
                    "настройки или удалите раздел из файла."
                ),
                "details": [],
            },
            status_code=403,
        )
    # Creating a project is the same settings mutation the «Проекты» section
    # locks behind edit mode — an import that references unknown project names
    # must not become a side door. Imports into existing projects stay open.
    missing_projects = await _missing_project_names(session, package)
    if missing_projects and not is_edit_mode(request):
        return templates.TemplateResponse(
            request,
            "partials/import_result.html",
            {
                "ok": False,
                "message": (
                    "Импорт создаст новые проекты ("
                    + ", ".join(missing_projects)
                    + ") — создание проектов доступно только в режиме "
                    "редактирования настроек. Разблокируйте настройки или "
                    "создайте проекты заранее."
                ),
                "details": [],
            },
            status_code=403,
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
