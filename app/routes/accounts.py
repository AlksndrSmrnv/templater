from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.routes.deps import SessionDep, TemplatesDep
from app.routes.uow import commit_and_refresh, commit_or_409
from app.schemas.entity import AccountCreate, AccountRead, AccountUpdate
from app.services.entities import AccountService

router = APIRouter()


@router.get("/accounts")
async def page_list(request: Request, templates: Jinja2Templates = TemplatesDep) -> Response:
    return templates.TemplateResponse(
        request,
        "entities/list.html",
        {"active": "data", "entity_type": "account", "title": "Счета"},
    )


@router.get("/accounts/new")
async def page_new(request: Request, templates: Jinja2Templates = TemplatesDep) -> Response:
    return templates.TemplateResponse(
        request,
        "entities/form.html",
        {"active": "data", "entity_type": "account", "title": "Новый счёт", "entity_id": None},
    )


@router.get("/accounts/{account_id}/edit")
async def page_edit(
    account_id: uuid.UUID, request: Request, templates: Jinja2Templates = TemplatesDep
) -> Response:
    return templates.TemplateResponse(
        request,
        "entities/form.html",
        {"active": "data", "entity_type": "account", "title": "Счёт", "entity_id": str(account_id)},
    )


@router.get("/api/accounts", response_model=list[AccountRead])
async def api_list(
    client_id: uuid.UUID | None = None, session: AsyncSession = SessionDep
) -> list[AccountRead]:
    items = await AccountService(session).list_all(client_id=client_id)
    return [AccountRead.model_validate(i, from_attributes=True) for i in items]


@router.get("/api/accounts/{account_id}", response_model=AccountRead)
async def api_get(account_id: uuid.UUID, session: AsyncSession = SessionDep) -> AccountRead:
    item = await AccountService(session).get(account_id)
    return AccountRead.model_validate(item, from_attributes=True)


@router.post("/api/accounts", response_model=AccountRead, status_code=201)
async def api_create(data: AccountCreate, session: AsyncSession = SessionDep) -> AccountRead:
    item = await commit_and_refresh(session, await AccountService(session).create(data))
    return AccountRead.model_validate(item, from_attributes=True)


@router.put("/api/accounts/{account_id}", response_model=AccountRead)
async def api_update(
    account_id: uuid.UUID, data: AccountUpdate, session: AsyncSession = SessionDep
) -> AccountRead:
    item = await commit_and_refresh(session, await AccountService(session).update(account_id, data))
    return AccountRead.model_validate(item, from_attributes=True)


@router.delete("/api/accounts/{account_id}", status_code=204)
async def api_delete(account_id: uuid.UUID, session: AsyncSession = SessionDep) -> Response:
    await AccountService(session).delete(account_id)
    await commit_or_409(session, message="Не удалось удалить счёт — есть связанные данные")
    return Response(status_code=204)
