from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ALL_ATTR_ENTITY_TYPES
from app.repositories.settings import SettingsRepository
from app.routes.deps import SessionDep, TemplatesDep
from app.routes.uow import commit_and_refresh, commit_or_409
from app.schemas.attribute import (
    AttributeDefinitionCreate,
    AttributeDefinitionRead,
    AttributeDefinitionUpdate,
)
from app.services.attribute_schema import AttributeSchemaService

router = APIRouter()


@router.get("/settings")
async def page_settings(request: Request, templates: Jinja2Templates = TemplatesDep) -> Response:
    s = get_settings()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "active": "settings",
            "llm_active": s.llm_active,
            "llm_model": s.gigachat_model,
            "entity_types": list(ALL_ATTR_ENTITY_TYPES),
        },
    )


@router.get("/api/attributes", response_model=list[AttributeDefinitionRead])
async def api_attrs_list(session: AsyncSession = SessionDep) -> list[AttributeDefinitionRead]:
    items = await AttributeSchemaService(session).list_all()
    return [AttributeDefinitionRead.model_validate(i, from_attributes=True) for i in items]


@router.post("/api/attributes", response_model=AttributeDefinitionRead, status_code=201)
async def api_attr_create(
    data: AttributeDefinitionCreate, session: AsyncSession = SessionDep
) -> AttributeDefinitionRead:
    svc = AttributeSchemaService(session)
    item = await commit_and_refresh(session, await svc.create(data))
    return AttributeDefinitionRead.model_validate(item, from_attributes=True)


@router.put("/api/attributes/{attr_id}", response_model=AttributeDefinitionRead)
async def api_attr_update(
    attr_id: uuid.UUID, data: AttributeDefinitionUpdate, session: AsyncSession = SessionDep
) -> AttributeDefinitionRead:
    svc = AttributeSchemaService(session)
    item = await commit_and_refresh(session, await svc.update(attr_id, data))
    return AttributeDefinitionRead.model_validate(item, from_attributes=True)


@router.post("/api/attributes/{attr_id}/deprecate", response_model=AttributeDefinitionRead)
async def api_attr_deprecate(
    attr_id: uuid.UUID, session: AsyncSession = SessionDep
) -> AttributeDefinitionRead:
    svc = AttributeSchemaService(session)
    item = await commit_and_refresh(session, await svc.deprecate(attr_id))
    return AttributeDefinitionRead.model_validate(item, from_attributes=True)


@router.get("/api/settings")
async def api_settings_get(session: AsyncSession = SessionDep) -> dict[str, Any]:
    return await SettingsRepository(session).all()


@router.put("/api/settings/{key}")
async def api_setting_set(
    key: str, value: dict[str, Any], session: AsyncSession = SessionDep
) -> dict[str, bool]:
    await SettingsRepository(session).set(key, value.get("value"))
    await commit_or_409(session)
    return {"ok": True}
