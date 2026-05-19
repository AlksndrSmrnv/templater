from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ALL_ATTR_ENTITY_TYPES, REFERENCE_TYPES
from app.repositories.attribute import AttributeDefinitionRepository
from app.routes.deps import SessionDep, TemplatesDep
from app.schemas.attribute import AttributeDefinitionRead
from app.schemas.reference import ReferenceValueCreate, ReferenceValueRead, ReferenceValueUpdate
from app.services.references import ReferenceService
from app.utils.errors import NotFoundError

router = APIRouter()


REFERENCE_TITLES = {
    "currency": "Валюты",
    "account_type": "Типы счетов",
    "card_type": "Типы карт",
    "bank": "Банки",
    "citizenship": "Гражданство",
}


def _check_ref_type(entity_type: str) -> None:
    if entity_type not in REFERENCE_TYPES:
        raise NotFoundError("Такого справочника не существует")


@router.get("/references")
async def page_index(request: Request, templates: Jinja2Templates = TemplatesDep):
    return templates.TemplateResponse(
        request,
        "references/index.html",
        {"active": "references", "references": REFERENCE_TITLES},
    )


@router.get("/references/{entity_type}")
async def page_list(entity_type: str, request: Request, templates: Jinja2Templates = TemplatesDep):
    _check_ref_type(entity_type)
    return templates.TemplateResponse(
        request,
        "references/list.html",
        {
            "active": "references",
            "entity_type": entity_type,
            "title": REFERENCE_TITLES[entity_type],
        },
    )


@router.get("/references/{entity_type}/new")
async def page_new(entity_type: str, request: Request, templates: Jinja2Templates = TemplatesDep):
    _check_ref_type(entity_type)
    return templates.TemplateResponse(
        request,
        "references/form.html",
        {
            "active": "references",
            "entity_type": entity_type,
            "title": REFERENCE_TITLES[entity_type],
            "value_id": None,
        },
    )


@router.get("/references/{entity_type}/{value_id}/edit")
async def page_edit(
    entity_type: str,
    value_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
):
    _check_ref_type(entity_type)
    return templates.TemplateResponse(
        request,
        "references/form.html",
        {
            "active": "references",
            "entity_type": entity_type,
            "title": REFERENCE_TITLES[entity_type],
            "value_id": str(value_id),
        },
    )


# ---------- JSON ----------

@router.get("/api/references/{entity_type}", response_model=list[ReferenceValueRead])
async def api_list(entity_type: str, session: AsyncSession = SessionDep):
    _check_ref_type(entity_type)
    items = await ReferenceService(session).list(entity_type)
    return [ReferenceValueRead.model_validate(i, from_attributes=True) for i in items]


@router.get("/api/references/{entity_type}/{value_id}", response_model=ReferenceValueRead)
async def api_get(entity_type: str, value_id: uuid.UUID, session: AsyncSession = SessionDep):
    _check_ref_type(entity_type)
    item = await ReferenceService(session).get(value_id)
    return ReferenceValueRead.model_validate(item, from_attributes=True)


@router.post("/api/references/{entity_type}", response_model=ReferenceValueRead, status_code=201)
async def api_create(entity_type: str, data: ReferenceValueCreate, session: AsyncSession = SessionDep):
    _check_ref_type(entity_type)
    if data.entity_type != entity_type:
        data = data.model_copy(update={"entity_type": entity_type})
    item = await ReferenceService(session).create(data)
    return ReferenceValueRead.model_validate(item, from_attributes=True)


@router.put("/api/references/{entity_type}/{value_id}", response_model=ReferenceValueRead)
async def api_update(
    entity_type: str,
    value_id: uuid.UUID,
    data: ReferenceValueUpdate,
    session: AsyncSession = SessionDep,
):
    _check_ref_type(entity_type)
    item = await ReferenceService(session).update(value_id, data)
    return ReferenceValueRead.model_validate(item, from_attributes=True)


@router.delete("/api/references/{entity_type}/{value_id}", status_code=204)
async def api_delete(entity_type: str, value_id: uuid.UUID, session: AsyncSession = SessionDep):
    _check_ref_type(entity_type)
    await ReferenceService(session).delete(value_id)
    return Response(status_code=204)


# ---------- Attribute schema ----------

@router.get("/api/attribute-schema/{entity_type}", response_model=list[AttributeDefinitionRead])
async def api_schema(entity_type: str, include_deprecated: bool = False, session: AsyncSession = SessionDep):
    if entity_type not in ALL_ATTR_ENTITY_TYPES:
        raise NotFoundError("Неизвестный тип сущности")
    repo = AttributeDefinitionRepository(session)
    items = await repo.list_by_entity(entity_type, include_deprecated=include_deprecated)
    return [AttributeDefinitionRead.model_validate(i, from_attributes=True) for i in items]
