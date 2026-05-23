from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData

from app.db.models import ALL_ATTR_ENTITY_TYPES, REFERENCE_TYPES, AttributeDefinition, ReferenceValue
from app.repositories.attribute import AttributeDefinitionRepository
from app.routes.deps import SessionDep, TemplatesDep
from app.routes.uow import commit_and_refresh, commit_or_409
from app.schemas.attribute import AttributeDefinitionRead
from app.schemas.reference import ReferenceValueCreate, ReferenceValueRead, ReferenceValueUpdate
from app.services.references import ReferenceService
from app.utils.errors import DomainError, NotFoundError

router = APIRouter()


REFERENCE_TITLES = {
    "currency": "💱 Валюты",
    "account_type": "🏷️ Типы счетов",
    "card_type": "💳 Типы карт",
    "bank": "🏦 Банки",
    "citizenship": "🪪 Гражданство",
}


def _check_ref_type(entity_type: str) -> None:
    if entity_type not in REFERENCE_TYPES:
        raise NotFoundError("Такого справочника не существует")


async def _active_schema(session: AsyncSession, entity_type: str) -> list[AttributeDefinition]:
    return await AttributeDefinitionRepository(session).list_by_entity(
        entity_type, include_deprecated=False
    )


def _form_str(form: FormData, key: str) -> str:
    value = form.get(key)
    return value if isinstance(value, str) else ""


def _read_attributes(form: FormData, schema: list[AttributeDefinition]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for field in schema:
        key = f"attr_{field.name}"
        if field.data_type == "bool":
            attrs[field.name] = _form_str(form, key).lower() in {"1", "true", "yes", "on"}
            continue
        value = _form_str(form, key)
        if value != "":
            attrs[field.name] = value
    return attrs


async def _reference_payload(
    entity_type: str,
    request: Request,
    session: AsyncSession,
) -> dict[str, Any]:
    schema = await _active_schema(session, entity_type)
    form = await request.form()
    return {
        "code": _form_str(form, "code"),
        "name": _form_str(form, "name"),
        "description": _form_str(form, "description"),
        "attributes": _read_attributes(form, schema),
    }


async def _reference_create_payload(
    entity_type: str, request: Request, session: AsyncSession
) -> ReferenceValueCreate:
    return ReferenceValueCreate(
        entity_type=entity_type,
        **await _reference_payload(entity_type, request, session),
    )


async def _reference_update_payload(
    entity_type: str, request: Request, session: AsyncSession
) -> ReferenceValueUpdate:
    return ReferenceValueUpdate(**await _reference_payload(entity_type, request, session))


def _filter_and_sort(
    items: list[ReferenceValue],
    schema: list[AttributeDefinition],
    *,
    search: str,
    sort: str,
    direction: str,
) -> list[ReferenceValue]:
    query = search.strip().lower()
    if query:
        items = [
            item
            for item in items
            if query in f"{item.code} {item.name} {item.description or ''}".lower()
        ]

    attr_names = {field.name for field in schema}
    sort_key = sort if sort in {"code", "name", "description"} or sort in attr_names else "code"

    def key(item: ReferenceValue) -> str:
        if sort_key in {"code", "name", "description"}:
            return str(getattr(item, sort_key) or "").lower()
        return str((item.attributes or {}).get(sort_key, "")).lower()

    return sorted(items, key=key, reverse=direction == "desc")


def _toast_header(message: str, *, toast_type: str = "success") -> str:
    return json.dumps({"showToast": {"message": message, "type": toast_type}})


def _form_errors_response(
    request: Request,
    templates: Jinja2Templates,
    message: str,
    *,
    details: Any | None = None,
    status_code: int = 422,
) -> Response:
    errors = details if isinstance(details, list) else []
    return templates.TemplateResponse(
        request,
        "partials/form_errors.html",
        {"message": message, "errors": errors},
        status_code=status_code,
    )


def _validation_errors_response(
    request: Request, templates: Jinja2Templates, exc: ValidationError
) -> Response:
    return _form_errors_response(
        request,
        templates,
        "Проверьте поля формы",
        details=[str(error["msg"]) for error in exc.errors()],
        status_code=422,
    )


@router.get("/references")
async def page_index(request: Request, templates: Jinja2Templates = TemplatesDep) -> Response:
    return templates.TemplateResponse(
        request,
        "references/index.html",
        {"active": "references", "references": REFERENCE_TITLES},
    )


@router.get("/references/{entity_type}")
async def page_list(
    entity_type: str,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    _check_ref_type(entity_type)
    schema = await _active_schema(session, entity_type)
    items = _filter_and_sort(
        await ReferenceService(session).list_by_type(entity_type),
        schema,
        search="",
        sort="code",
        direction="asc",
    )
    return templates.TemplateResponse(
        request,
        "references/list.html",
        {
            "active": "references",
            "entity_type": entity_type,
            "title": REFERENCE_TITLES[entity_type],
            "schema": schema,
            "items": items,
            "sort": "code",
            "direction": "asc",
            "search": "",
        },
    )


@router.get("/references/{entity_type}/new")
async def page_new(
    entity_type: str,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    _check_ref_type(entity_type)
    return templates.TemplateResponse(
        request,
        "references/form.html",
        {
            "active": "references",
            "entity_type": entity_type,
            "title": REFERENCE_TITLES[entity_type],
            "value_id": None,
            "schema": await _active_schema(session, entity_type),
            "item": None,
        },
    )


@router.get("/references/{entity_type}/{value_id}/edit")
async def page_edit(
    entity_type: str,
    value_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    _check_ref_type(entity_type)
    item = await ReferenceService(session).get_typed(entity_type, value_id)
    return templates.TemplateResponse(
        request,
        "references/form.html",
        {
            "active": "references",
            "entity_type": entity_type,
            "title": REFERENCE_TITLES[entity_type],
            "value_id": str(value_id),
            "schema": await _active_schema(session, entity_type),
            "item": item,
        },
    )


# ---------- HTMX ----------

@router.get("/references-htmx/{entity_type}/table")
async def htmx_table(
    entity_type: str,
    request: Request,
    search: str = "",
    sort: str = "code",
    direction: str = "asc",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    _check_ref_type(entity_type)
    schema = await _active_schema(session, entity_type)
    items = _filter_and_sort(
        await ReferenceService(session).list_by_type(entity_type),
        schema,
        search=search,
        sort=sort,
        direction=direction,
    )
    return templates.TemplateResponse(
        request,
        "partials/reference_table.html",
        {
            "entity_type": entity_type,
            "schema": schema,
            "items": items,
        },
    )


@router.post("/references-htmx/{entity_type}")
async def htmx_create(
    entity_type: str,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    _check_ref_type(entity_type)
    try:
        data = await _reference_create_payload(entity_type, request, session)
        await commit_and_refresh(session, await ReferenceService(session).create(data))
    except ValidationError as exc:
        return _validation_errors_response(request, templates, exc)
    except DomainError as exc:
        return _form_errors_response(
            request, templates, exc.message, details=exc.details, status_code=exc.status_code
        )
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": f"/references/{entity_type}",
            "HX-Trigger": _toast_header("Сохранено"),
        },
    )


@router.put("/references-htmx/{entity_type}/{value_id}")
async def htmx_update(
    entity_type: str,
    value_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    _check_ref_type(entity_type)
    try:
        data = await _reference_update_payload(entity_type, request, session)
        await commit_and_refresh(
            session,
            await ReferenceService(session).update(value_id, data, entity_type=entity_type),
        )
    except ValidationError as exc:
        return _validation_errors_response(request, templates, exc)
    except DomainError as exc:
        return _form_errors_response(
            request, templates, exc.message, details=exc.details, status_code=exc.status_code
        )
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": f"/references/{entity_type}",
            "HX-Trigger": _toast_header("Сохранено"),
        },
    )


@router.delete("/references-htmx/{entity_type}/{value_id}")
async def htmx_delete(
    entity_type: str,
    value_id: uuid.UUID,
    redirect: bool = Query(False),
    session: AsyncSession = SessionDep,
) -> Response:
    _check_ref_type(entity_type)
    await ReferenceService(session).delete(value_id, entity_type=entity_type)
    await commit_or_409(session, message="Не удалось удалить запись справочника — есть связанные данные")
    headers = {"HX-Trigger": _toast_header("Удалено")}
    if redirect:
        headers["HX-Redirect"] = f"/references/{entity_type}"
    return Response(status_code=204 if redirect else 200, headers=headers)


# ---------- JSON ----------

@router.get("/api/references/{entity_type}", response_model=list[ReferenceValueRead])
async def api_list(entity_type: str, session: AsyncSession = SessionDep) -> list[ReferenceValueRead]:
    _check_ref_type(entity_type)
    items = await ReferenceService(session).list_by_type(entity_type)
    return [ReferenceValueRead.model_validate(i, from_attributes=True) for i in items]


@router.get("/api/references/{entity_type}/{value_id}", response_model=ReferenceValueRead)
async def api_get(
    entity_type: str, value_id: uuid.UUID, session: AsyncSession = SessionDep
) -> ReferenceValueRead:
    _check_ref_type(entity_type)
    item = await ReferenceService(session).get_typed(entity_type, value_id)
    return ReferenceValueRead.model_validate(item, from_attributes=True)


@router.post("/api/references/{entity_type}", response_model=ReferenceValueRead, status_code=201)
async def api_create(
    entity_type: str, data: ReferenceValueCreate, session: AsyncSession = SessionDep
) -> ReferenceValueRead:
    _check_ref_type(entity_type)
    if data.entity_type != entity_type:
        data = data.model_copy(update={"entity_type": entity_type})
    item = await commit_and_refresh(session, await ReferenceService(session).create(data))
    return ReferenceValueRead.model_validate(item, from_attributes=True)


@router.put("/api/references/{entity_type}/{value_id}", response_model=ReferenceValueRead)
async def api_update(
    entity_type: str,
    value_id: uuid.UUID,
    data: ReferenceValueUpdate,
    session: AsyncSession = SessionDep,
) -> ReferenceValueRead:
    _check_ref_type(entity_type)
    item = await commit_and_refresh(
        session,
        await ReferenceService(session).update(value_id, data, entity_type=entity_type),
    )
    return ReferenceValueRead.model_validate(item, from_attributes=True)


@router.delete("/api/references/{entity_type}/{value_id}", status_code=204)
async def api_delete(entity_type: str, value_id: uuid.UUID, session: AsyncSession = SessionDep) -> Response:
    _check_ref_type(entity_type)
    await ReferenceService(session).delete(value_id, entity_type=entity_type)
    await commit_or_409(session, message="Не удалось удалить запись справочника — есть связанные данные")
    return Response(status_code=204)


# ---------- Attribute schema ----------

@router.get("/api/attribute-schema/{entity_type}", response_model=list[AttributeDefinitionRead])
async def api_schema(
    entity_type: str, include_deprecated: bool = False, session: AsyncSession = SessionDep
) -> list[AttributeDefinitionRead]:
    if entity_type not in ALL_ATTR_ENTITY_TYPES:
        raise NotFoundError("Неизвестный тип сущности")
    repo = AttributeDefinitionRepository(session)
    items = await repo.list_by_entity(entity_type, include_deprecated=include_deprecated)
    return [AttributeDefinitionRead.model_validate(i, from_attributes=True) for i in items]
