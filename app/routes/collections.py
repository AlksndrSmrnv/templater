from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.routes.deps import SessionDep, TemplatesDep
from app.routes.htmx_utils import form_str, toast_header
from app.routes.uow import commit_or_409
from app.services.collections import CollectionService
from app.services.projects import ProjectService
from app.utils.errors import DomainError

router = APIRouter()


async def _tree_response(
    request: Request,
    templates: Jinja2Templates,
    session: AsyncSession,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    context = await CollectionService(session).build_workspace_tree()
    return templates.TemplateResponse(
        request,
        "partials/collections_tree.html",
        context,
        headers=headers,
    )


def _parse_path(raw: str) -> list[str]:
    """Decode a folder path sent as a JSON array of segments (empty/absent ⇒ root)."""

    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [str(seg).strip() for seg in value if str(seg).strip()]


def _parse_uuids(raw: str) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.append(uuid.UUID(item))
        except ValueError:
            continue
    return out


@router.post("/collections/import-htmx")
async def htmx_import_collection(
    request: Request,
    file: UploadFile = File(...),
    project_id: str = Form(""),
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    # Every template belongs to exactly one project, so the import form must
    # name the target project for the whole collection.
    try:
        target_project = await ProjectService(session).get(uuid.UUID(project_id.strip()))
    except (ValueError, DomainError):
        return await _tree_response(
            request,
            templates,
            session,
            headers={"HX-Trigger": toast_header("Выберите проект для импорта", toast_type="error")},
        )
    try:
        raw = await file.read()
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return await _tree_response(
            request,
            templates,
            session,
            headers={"HX-Trigger": toast_header(f"Не удалось прочитать файл: {exc}", toast_type="error")},
        )
    try:
        summary = await CollectionService(session).import_collection(
            data, project_id=target_project.id
        )
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return await _tree_response(
            request,
            templates,
            session,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    message = f"Импортирована коллекция «{summary.name}»: {summary.templates_created} шаблон(ов)"
    if summary.unparsable:
        message += f", из них без разбираемого тела: {summary.unparsable}"
    return await _tree_response(
        request,
        templates,
        session,
        headers={"HX-Trigger": toast_header(message)},
    )


@router.post("/collections/{collection_id}/process-llm")
async def htmx_process_collection(
    collection_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    try:
        summary = await CollectionService(session).process_collection_llm(collection_id)
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return await _tree_response(
            request,
            templates,
            session,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    message = (
        f"Обработано: {summary.processed}"
        f" · пропущено: {summary.skipped}"
        f" · ошибок: {summary.failed}"
    )
    return await _tree_response(
        request,
        templates,
        session,
        headers={"HX-Trigger": toast_header(message)},
    )


@router.delete("/collections/{collection_id}")
async def htmx_delete_collection(
    collection_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    removed = await CollectionService(session).delete(collection_id)
    await commit_or_409(session)
    return await _tree_response(
        request,
        templates,
        session,
        headers={"HX-Trigger": toast_header(f"Коллекция удалена ({removed} шаблон(ов))")},
    )


async def _create_folder(
    collection_id: uuid.UUID | None,
    request: Request,
    templates: Jinja2Templates,
    session: AsyncSession,
) -> Response:
    """Shared body for collection and root folder creation. ``collection_id is
    None`` targets the root space."""

    form = await request.form()
    parent = _parse_path(form_str(form, "parent"))
    name = form_str(form, "name")
    try:
        await CollectionService(session).create_folder(collection_id, parent, name)
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return await _tree_response(
            request,
            templates,
            session,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    return await _tree_response(
        request,
        templates,
        session,
        headers={"HX-Trigger": toast_header(f"Папка «{name.strip()}» создана")},
    )


async def _rename_folder(
    collection_id: uuid.UUID | None,
    request: Request,
    templates: Jinja2Templates,
    session: AsyncSession,
) -> Response:
    form = await request.form()
    path = _parse_path(form_str(form, "path"))
    name = form_str(form, "name")
    try:
        await CollectionService(session).rename_folder(collection_id, path, name)
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return await _tree_response(
            request,
            templates,
            session,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    return await _tree_response(
        request,
        templates,
        session,
        headers={"HX-Trigger": toast_header("Папка переименована")},
    )


async def _delete_folder(
    collection_id: uuid.UUID | None,
    request: Request,
    templates: Jinja2Templates,
    session: AsyncSession,
) -> Response:
    # htmx 2.x encodes DELETE params (hx-vals) in the URL query string (config
    # ``methodsThatUseUrlParams`` defaults to ['get','delete']); fall back to the
    # form body for safety.
    raw = request.query_params.get("path", "")
    if not raw:
        raw = form_str(await request.form(), "path")
    path = _parse_path(raw)
    try:
        await CollectionService(session).delete_folder(collection_id, path)
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return await _tree_response(
            request,
            templates,
            session,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    return await _tree_response(
        request,
        templates,
        session,
        headers={"HX-Trigger": toast_header("Папка удалена")},
    )


# Root-folder routes MUST be declared before the ``/collections/{collection_id}``
# ones: FastAPI captures ``{collection_id}`` as a string at routing time and only
# validates it as a UUID afterwards, so the literal ``root`` would otherwise match
# the parametrized route and 422 before reaching these handlers.
@router.post("/collections/root/folders")
async def htmx_create_root_folder(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return await _create_folder(None, request, templates, session)


@router.post("/collections/root/folders/rename")
async def htmx_rename_root_folder(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return await _rename_folder(None, request, templates, session)


@router.delete("/collections/root/folders")
async def htmx_delete_root_folder(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return await _delete_folder(None, request, templates, session)


@router.post("/collections/{collection_id}/folders")
async def htmx_create_folder(
    collection_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return await _create_folder(collection_id, request, templates, session)


@router.post("/collections/{collection_id}/folders/rename")
async def htmx_rename_folder(
    collection_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return await _rename_folder(collection_id, request, templates, session)


@router.delete("/collections/{collection_id}/folders")
async def htmx_delete_folder(
    collection_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return await _delete_folder(collection_id, request, templates, session)


@router.post("/collections/move-request")
async def htmx_move_request(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    form = await request.form()
    raw_template_id = form_str(form, "template_id")
    try:
        template_id = uuid.UUID(raw_template_id)
    except ValueError:
        return await _tree_response(
            request,
            templates,
            session,
            headers={"HX-Trigger": toast_header("Некорректный запрос", toast_type="error")},
        )
    # Empty collection_id means "Без коллекции" (ungrouped) — intentional. A
    # non-empty but malformed value is a broken payload, not a move to ungrouped.
    raw_collection = form_str(form, "collection_id").strip()
    target_collection_id: uuid.UUID | None = None
    if raw_collection:
        try:
            target_collection_id = uuid.UUID(raw_collection)
        except ValueError:
            return await _tree_response(
                request,
                templates,
                session,
                headers={"HX-Trigger": toast_header("Некорректный запрос", toast_type="error")},
            )
    folder = _parse_path(form_str(form, "folder"))
    order = _parse_uuids(form_str(form, "order"))
    try:
        await CollectionService(session).move_request(
            template_id, target_collection_id, folder, order
        )
        await commit_or_409(session)
    except DomainError as exc:
        await session.rollback()
        return await _tree_response(
            request,
            templates,
            session,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    return await _tree_response(request, templates, session)
