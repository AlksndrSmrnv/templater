from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.repositories.settings import SettingsRepository
from app.routes.deps import SessionDep, TemplatesDep
from app.routes.htmx_utils import (
    form_bool,
    form_errors_response,
    form_str,
    toast_header,
    validation_errors_response,
)
from app.routes.uow import commit_and_refresh, commit_or_409
from app.schemas.attribute import (
    ALLOWED_TYPES,
    AttributeDefinitionCreate,
    AttributeDefinitionUpdate,
    AttributeReorder,
)
from app.services.attribute_schema import AttributeSchemaService
from app.services.reference_types import ReferenceTypeService
from app.utils.errors import DomainError

router = APIRouter()


@router.get("/settings")
async def page_settings(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    s = get_settings()
    saved_policy = await SettingsRepository(session).get("import_policy", "skip")
    default_policy = saved_policy if saved_policy in {"skip", "overwrite", "fail"} else "skip"
    svc = AttributeSchemaService(session)
    attributes = await svc.list_all()
    usage_counts = await svc.usage(attributes)
    entity_types = await ReferenceTypeService(session).all_attr_entity_types()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active": "settings",
            "llm_active": s.llm_active,
            "llm_model": s.gigachat_model,
            "entity_types": entity_types,
            "attributes": attributes,
            "usage_counts": usage_counts,
            "selected_entity_type": "",
            "default_policy": default_policy,
        },
    )


def _attribute_options(form: Any) -> dict[str, Any]:
    data_type = form_str(form, "data_type")
    if data_type == "enum":
        values = [value.strip() for value in form_str(form, "enum_values").split(",") if value.strip()]
        return {"values": values}
    if data_type == "ref":
        return {"ref_entity": form_str(form, "ref_entity")}
    return {}


async def _attribute_create_payload(request: Request) -> AttributeDefinitionCreate:
    form = await request.form()
    data_type = form_str(form, "data_type")
    return AttributeDefinitionCreate(
        entity_type=form_str(form, "entity_type"),
        name=form_str(form, "name"),
        label=form_str(form, "label"),
        data_type=data_type,
        is_required=form_bool(form, "is_required"),
        display_order=int(form_str(form, "display_order") or 0),
        description=form_str(form, "description"),
        options=_attribute_options(form),
    )


async def _attribute_update_payload(request: Request, current_data_type: str) -> AttributeDefinitionUpdate:
    form = await request.form()
    display_order = int(form_str(form, "display_order") or 0)
    data_type = current_data_type
    options: dict[str, Any]
    if data_type == "enum":
        values = [value.strip() for value in form_str(form, "enum_values").split(",") if value.strip()]
        options = {"values": values}
    elif data_type == "ref":
        options = {"ref_entity": form_str(form, "ref_entity")}
    else:
        options = {}
    return AttributeDefinitionUpdate(
        label=form_str(form, "label"),
        is_required=form_bool(form, "is_required"),
        display_order=display_order,
        description=form_str(form, "description"),
        options=options,
    )


@router.get("/settings-htmx/attributes/table")
async def htmx_attributes_table(
    request: Request,
    entity_type: str = "",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    svc = AttributeSchemaService(session)
    items = await svc.list_all()
    if entity_type:
        items = [item for item in items if item.entity_type == entity_type]
    usage_counts = await svc.usage(items)
    return templates.TemplateResponse(
        request,
        "partials/attributes_table.html",
        {"attributes": items, "usage_counts": usage_counts},
    )


@router.get("/settings-htmx/attributes/new")
async def htmx_attribute_new(
    request: Request,
    entity_type: str = "",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    ref_svc = ReferenceTypeService(session)
    return templates.TemplateResponse(
        request,
        "partials/attribute_form.html",
        {
            "attribute": None,
            "entity_types": await ref_svc.all_attr_entity_types(),
            "reference_types": await ref_svc.codes(),
            "data_types": sorted(ALLOWED_TYPES),
            "selected_entity_type": entity_type,
        },
    )


@router.get("/settings-htmx/attributes/{attr_id}/edit")
async def htmx_attribute_edit(
    attr_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    svc = AttributeSchemaService(session)
    try:
        attribute = await svc.get(attr_id)
    except DomainError as exc:
        return form_errors_response(
            request,
            templates,
            exc.message,
            details=exc.details,
            status_code=exc.status_code,
        )
    ref_svc = ReferenceTypeService(session)
    return templates.TemplateResponse(
        request,
        "partials/attribute_form.html",
        {
            "attribute": attribute,
            "entity_types": await ref_svc.all_attr_entity_types(),
            "reference_types": await ref_svc.codes(),
            "data_types": sorted(ALLOWED_TYPES),
            "selected_entity_type": attribute.entity_type,
        },
    )


@router.post("/settings-htmx/attributes")
async def htmx_attribute_create(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    svc = AttributeSchemaService(session)
    try:
        data = await _attribute_create_payload(request)
        await commit_and_refresh(session, await svc.create(data))
    except ValueError:
        return form_errors_response(request, templates, "Порядок должен быть числом")
    except DomainError as exc:
        return form_errors_response(
            request, templates, exc.message, details=exc.details, status_code=exc.status_code
        )
    except ValidationError as exc:
        return validation_errors_response(request, templates, exc)
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Атрибут сохранён", close_modal=True, refresh_attributes=True)},
    )


@router.put("/settings-htmx/attributes/{attr_id}")
async def htmx_attribute_update(
    attr_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    svc = AttributeSchemaService(session)
    try:
        attribute = await svc.get(attr_id)
        data = await _attribute_update_payload(request, attribute.data_type)
        await commit_and_refresh(session, await svc.update(attr_id, data))
    except ValueError:
        return form_errors_response(request, templates, "Порядок должен быть числом")
    except DomainError as exc:
        return form_errors_response(
            request, templates, exc.message, details=exc.details, status_code=exc.status_code
        )
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Атрибут сохранён", close_modal=True, refresh_attributes=True)},
    )


@router.delete("/settings-htmx/attributes/{attr_id}")
async def htmx_attribute_delete(
    attr_id: uuid.UUID, session: AsyncSession = SessionDep
) -> Response:
    svc = AttributeSchemaService(session)
    try:
        await svc.delete(attr_id)
    except DomainError as exc:
        return Response(
            status_code=exc.status_code,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    await commit_or_409(session, message="Не удалось удалить атрибут")
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Атрибут удалён", refresh_attributes=True)},
    )


@router.post("/settings-htmx/attributes/reorder")
async def htmx_attribute_reorder(
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    form = await request.form()
    raw_order = [item for item in form_str(form, "order").split(",") if item.strip()]
    try:
        data = AttributeReorder(entity_type=form_str(form, "entity_type"), order=raw_order)
    except ValidationError:
        return form_errors_response(request, templates, "Некорректный список атрибутов")
    svc = AttributeSchemaService(session)
    try:
        await svc.reorder(data.entity_type, data.order)
    except DomainError as exc:
        return Response(
            status_code=exc.status_code,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    await commit_or_409(session, message="Не удалось изменить порядок атрибутов")
    return Response(
        status_code=204,
        headers={"HX-Trigger": toast_header("Порядок обновлён", refresh_attributes=True)},
    )


@router.put("/settings-htmx/import_policy")
async def htmx_import_policy(request: Request, session: AsyncSession = SessionDep) -> Response:
    form = await request.form()
    policy = form_str(form, "import_policy")
    if policy not in {"skip", "overwrite", "fail"}:
        policy = "skip"
    await SettingsRepository(session).set("import_policy", policy)
    await commit_or_409(session)
    return Response(status_code=204, headers={"HX-Redirect": "/templater/settings?saved=1"})
