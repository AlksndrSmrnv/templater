from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.routes.deps import SessionDep, TemplatesDep
from app.schemas.entity import ClientCreate, ClientRead, ClientUpdate
from app.services.entities import ClientService

router = APIRouter()


# ---------- HTML pages ----------

@router.get("/clients")
async def page_list(request: Request, templates: Jinja2Templates = TemplatesDep):
    return templates.TemplateResponse(
        request,
        "entities/list.html",
        {"active": "data", "entity_type": "client", "title": "Клиенты"},
    )


@router.get("/clients/new")
async def page_new(request: Request, templates: Jinja2Templates = TemplatesDep):
    return templates.TemplateResponse(
        request,
        "entities/form.html",
        {"active": "data", "entity_type": "client", "title": "Новый клиент", "entity_id": None},
    )


@router.get("/clients/{client_id}/edit")
async def page_edit(client_id: uuid.UUID, request: Request, templates: Jinja2Templates = TemplatesDep):
    return templates.TemplateResponse(
        request,
        "entities/form.html",
        {"active": "data", "entity_type": "client", "title": "Клиент", "entity_id": str(client_id)},
    )


# ---------- JSON API ----------

@router.get("/api/clients", response_model=list[ClientRead])
async def api_list(session: AsyncSession = SessionDep):
    items = await ClientService(session).list()
    return [ClientRead.model_validate(i, from_attributes=True) for i in items]


@router.get("/api/clients/{client_id}", response_model=ClientRead)
async def api_get(client_id: uuid.UUID, session: AsyncSession = SessionDep):
    item = await ClientService(session).get(client_id)
    return ClientRead.model_validate(item, from_attributes=True)


@router.post("/api/clients", response_model=ClientRead, status_code=201)
async def api_create(data: ClientCreate, session: AsyncSession = SessionDep):
    item = await ClientService(session).create(data)
    return ClientRead.model_validate(item, from_attributes=True)


@router.put("/api/clients/{client_id}", response_model=ClientRead)
async def api_update(client_id: uuid.UUID, data: ClientUpdate, session: AsyncSession = SessionDep):
    item = await ClientService(session).update(client_id, data)
    return ClientRead.model_validate(item, from_attributes=True)


@router.delete("/api/clients/{client_id}", status_code=204)
async def api_delete(client_id: uuid.UUID, session: AsyncSession = SessionDep):
    await ClientService(session).delete(client_id)
    return Response(status_code=204)
