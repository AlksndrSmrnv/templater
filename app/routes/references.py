from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AttributeDefinition, ReferenceType, ReferenceValue
from app.repositories.attribute import AttributeDefinitionRepository
from app.repositories.reference_type import ReferenceTypeRepository
from app.routes.deps import SessionDep, TemplatesDep
from app.routes.htmx_utils import (
    form_errors_response,
    form_str,
    read_entity_attributes,
    toast_header,
    validation_errors_response,
)
from app.routes.uow import commit_and_refresh, commit_or_409
from app.schemas.reference import ReferenceValueCreate, ReferenceValueUpdate
from app.services.reference_types import ColumnSpec, ReferenceTypeService
from app.services.references import ReferenceService
from app.utils.errors import DomainError, NotFoundError

router = APIRouter()

# Data types offered when defining columns of a new reference type from the UI.
# (ref/datetime are intentionally omitted here — add them later via Settings.)
NEW_TYPE_COLUMN_TYPES = ["string", "int", "number", "bool", "date", "enum"]


def _ref_title(ref_type: ReferenceType) -> str:
    return f"{ref_type.icon} {ref_type.title}".strip()


async def _require_ref_type(session: AsyncSession, entity_type: str) -> ReferenceType:
    ref_type = await ReferenceTypeRepository(session).get(entity_type)
    if ref_type is None:
        raise NotFoundError("Такого справочника не существует")
    return ref_type


async def _active_schema(session: AsyncSession, entity_type: str) -> list[AttributeDefinition]:
    return await AttributeDefinitionRepository(session).list_by_entity(entity_type)


async def _reference_payload(
    entity_type: str,
    request: Request,
    session: AsyncSession,
) -> dict[str, Any]:
    schema = await _active_schema(session, entity_type)
    form = await request.form()
    return {
        "code": form_str(form, "code"),
        "name": form_str(form, "name"),
        "description": form_str(form, "description"),
        "attributes": read_entity_attributes(form, schema),
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


@router.get("/references")
async def page_index(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    return templates.TemplateResponse(
        request,
        "references/index.html",
        {
            "active": "references",
            "references": await ReferenceTypeService(session).list_all(),
        },
    )


@router.get("/references/new-type")
async def page_new_type(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
) -> Response:
    return templates.TemplateResponse(
        request,
        "references/new_type.html",
        {"active": "references", "column_types": NEW_TYPE_COLUMN_TYPES},
    )


@router.get("/references/{entity_type}")
async def page_list(
    entity_type: str,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    ref_type = await _require_ref_type(session, entity_type)
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
            "title": _ref_title(ref_type),
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
    ref_type = await _require_ref_type(session, entity_type)
    return templates.TemplateResponse(
        request,
        "references/form.html",
        {
            "active": "references",
            "entity_type": entity_type,
            "title": _ref_title(ref_type),
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
    ref_type = await _require_ref_type(session, entity_type)
    item = await ReferenceService(session).get_typed(entity_type, value_id)
    return templates.TemplateResponse(
        request,
        "references/form.html",
        {
            "active": "references",
            "entity_type": entity_type,
            "title": _ref_title(ref_type),
            "value_id": str(value_id),
            "schema": await _active_schema(session, entity_type),
            "item": item,
        },
    )


# ---------- HTMX ----------

def _parse_columns(form: Any) -> list[ColumnSpec]:
    """Read the repeated column fields from the new-reference-type form.

    The Alpine repeater submits parallel lists ``col_name`` / ``col_label`` /
    ``col_type`` / ``col_required`` / ``col_enum``; blank rows (no name) are
    dropped so an empty trailing row doesn't error.
    """

    names = form.getlist("col_name")
    labels = form.getlist("col_label")
    types = form.getlist("col_type")
    enums = form.getlist("col_enum")
    required = set(form.getlist("col_required"))  # checkbox value = row index

    columns: list[ColumnSpec] = []
    for i, raw_name in enumerate(names):
        name = (raw_name or "").strip()
        if not name:
            continue
        data_type = (types[i] if i < len(types) else "string") or "string"
        enum_raw = enums[i] if i < len(enums) else ""
        columns.append(
            ColumnSpec(
                name=name,
                label=(labels[i] if i < len(labels) else "").strip(),
                data_type=data_type,
                is_required=str(i) in required,
                enum_values=[v.strip() for v in enum_raw.split(",") if v.strip()],
            )
        )
    return columns


@router.post("/references-htmx/types")
async def htmx_create_type(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    form = await request.form()
    try:
        ref_type = await ReferenceTypeService(session).create(
            code=form_str(form, "code"),
            title=form_str(form, "title"),
            icon=form_str(form, "icon"),
            description=form_str(form, "description"),
            columns=_parse_columns(form),
        )
        code = ref_type.code  # capture before commit expires the instance
        await commit_or_409(session, message="Не удалось создать справочник")
    except ValidationError as exc:
        # A column with e.g. an over-long label trips Pydantic inside the service.
        await session.rollback()
        return validation_errors_response(request, templates, exc)
    except DomainError as exc:
        await session.rollback()
        return form_errors_response(
            request, templates, exc.message, details=exc.details, status_code=exc.status_code
        )
    return Response(
        status_code=204,
        headers={"HX-Redirect": f"/templater/references/{code}"},
    )


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
    await _require_ref_type(session, entity_type)
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
    await _require_ref_type(session, entity_type)
    try:
        data = await _reference_create_payload(entity_type, request, session)
        created = await commit_and_refresh(session, await ReferenceService(session).create(data))
    except ValidationError as exc:
        return validation_errors_response(request, templates, exc)
    except DomainError as exc:
        return form_errors_response(
            request, templates, exc.message, details=exc.details, status_code=exc.status_code
        )
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": f"/templater/references/{entity_type}/{created.id}/edit?saved=1",
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
    await _require_ref_type(session, entity_type)
    try:
        data = await _reference_update_payload(entity_type, request, session)
        await commit_and_refresh(
            session,
            await ReferenceService(session).update(value_id, data, entity_type=entity_type),
        )
    except ValidationError as exc:
        return validation_errors_response(request, templates, exc)
    except DomainError as exc:
        return form_errors_response(
            request, templates, exc.message, details=exc.details, status_code=exc.status_code
        )
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": f"/templater/references/{entity_type}/{value_id}/edit?saved=1",
        },
    )


@router.delete("/references-htmx/{entity_type}/{value_id}")
async def htmx_delete(
    entity_type: str,
    value_id: uuid.UUID,
    redirect: bool = Query(False),
    session: AsyncSession = SessionDep,
) -> Response:
    await _require_ref_type(session, entity_type)
    await ReferenceService(session).delete(value_id, entity_type=entity_type)
    await commit_or_409(session, message="Не удалось удалить запись справочника — есть связанные данные")
    headers = {"HX-Trigger": toast_header("Удалено")}
    if redirect:
        headers["HX-Redirect"] = f"/templater/references/{entity_type}"
    return Response(status_code=204 if redirect else 200, headers=headers)
