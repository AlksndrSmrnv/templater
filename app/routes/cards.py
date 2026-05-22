from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.routes.deps import SessionDep, TemplatesDep
from app.routes.uow import commit_and_refresh, commit_or_409
from app.schemas.entity import CardCreate, CardRead, CardUpdate
from app.services.entities import CardService

router = APIRouter()


@router.get("/cards")
async def page_list(request: Request, templates: Jinja2Templates = TemplatesDep) -> Response:
    return templates.TemplateResponse(
        request,
        "entities/list.html",
        {"active": "data", "entity_type": "card", "title": "Карты"},
    )


@router.get("/cards/new")
async def page_new(request: Request, templates: Jinja2Templates = TemplatesDep) -> Response:
    return templates.TemplateResponse(
        request,
        "entities/form.html",
        {"active": "data", "entity_type": "card", "title": "Новая карта", "entity_id": None},
    )


@router.get("/cards/{card_id}/edit")
async def page_edit(
    card_id: uuid.UUID, request: Request, templates: Jinja2Templates = TemplatesDep
) -> Response:
    return templates.TemplateResponse(
        request,
        "entities/form.html",
        {"active": "data", "entity_type": "card", "title": "Карта", "entity_id": str(card_id)},
    )


@router.get("/api/cards", response_model=list[CardRead])
async def api_list(
    account_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    session: AsyncSession = SessionDep,
) -> list[CardRead]:
    items = await CardService(session).list_all(account_id=account_id, client_id=client_id)
    return [CardRead.model_validate(i, from_attributes=True) for i in items]


@router.get("/api/cards/{card_id}", response_model=CardRead)
async def api_get(card_id: uuid.UUID, session: AsyncSession = SessionDep) -> CardRead:
    item = await CardService(session).get(card_id)
    return CardRead.model_validate(item, from_attributes=True)


@router.post("/api/cards", response_model=CardRead, status_code=201)
async def api_create(data: CardCreate, session: AsyncSession = SessionDep) -> CardRead:
    item = await commit_and_refresh(session, await CardService(session).create(data))
    return CardRead.model_validate(item, from_attributes=True)


@router.put("/api/cards/{card_id}", response_model=CardRead)
async def api_update(card_id: uuid.UUID, data: CardUpdate, session: AsyncSession = SessionDep) -> CardRead:
    item = await commit_and_refresh(session, await CardService(session).update(card_id, data))
    return CardRead.model_validate(item, from_attributes=True)


@router.delete("/api/cards/{card_id}", status_code=204)
async def api_delete(card_id: uuid.UUID, session: AsyncSession = SessionDep) -> Response:
    await CardService(session).delete(card_id)
    await commit_or_409(session)
    return Response(status_code=204)
