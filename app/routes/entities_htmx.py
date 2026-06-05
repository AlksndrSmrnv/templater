from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any, cast

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DATA_ENTITY_TYPES, Account, AttributeDefinition, Card
from app.routes.deps import SessionDep, TemplatesDep
from app.routes.htmx_utils import (
    form_errors_response,
    form_str,
    read_entity_attributes,
    toast_header,
    validation_errors_response,
)
from app.routes.uow import commit_and_refresh, commit_or_409
from app.schemas.entity import (
    AccountCreate,
    AccountUpdate,
    CardCreate,
    CardUpdate,
    ClientCreate,
    ClientUpdate,
)
from app.schemas.exchange import ExportRequest
from app.services.attribute_schema import AttributeSchemaService
from app.services.entities import AccountService, CardService, ClientService
from app.services.export_import import ExportImportService
from app.utils.errors import DomainError, NotFoundError

router = APIRouter()

ENTITY_TITLES = {
    "client": "Клиенты",
    "account": "Счета",
    "card": "Карты",
}
ENTITY_ICONS = {
    "client": "lucide:users",
    "account": "lucide:landmark",
    "card": "lucide:credit-card",
}
RELATION_COLUMNS = {
    "client": ("accounts", "cards"),
    "account": ("client", "cards"),
    "card": ("client", "account"),
}
SERVICE_MAP = {
    "client": ClientService,
    "account": AccountService,
    "card": CardService,
}

# Rows per page for the entity list. Search/filter/sort/pagination run in SQL, so
# only this many rows are ever loaded and rendered per request.
PAGE_SIZE = 50


def _service(session: AsyncSession, entity_type: str) -> Any:
    return SERVICE_MAP[entity_type](session)


def _page_param(request: Request | None) -> int:
    if request is None:
        return 1
    raw = request.query_params.get("page")
    try:
        return max(1, int(raw)) if raw else 1
    except (TypeError, ValueError):
        return 1


def check_entity_type(entity_type: str) -> None:
    if entity_type not in DATA_ENTITY_TYPES:
        raise NotFoundError("Неизвестный тип сущности")


def entity_label(entity_type: str, row: Any) -> str:
    attrs = row.attributes or {}
    row_id = str(row.id)
    if entity_type == "client":
        return (
            attrs.get("fullName")
            or attrs.get("name")
            or attrs.get("shortName")
            or attrs.get("inn")
            or row.description
            or row_id[:8]
        )
    if entity_type in {"account", "card"}:
        return attrs.get("number") or row.description or row_id[:8]
    return row.description or row_id[:8]


async def _schema(session: AsyncSession, entity_type: str) -> list[AttributeDefinition]:
    return await AttributeSchemaService(session).list_schema(entity_type)


async def _get_item(session: AsyncSession, entity_type: str, item_id: uuid.UUID) -> Any:
    return await _service(session, entity_type).get(item_id)


async def _relations(
    session: AsyncSession,
    entity_type: str,
    *,
    items: list[Any] | None = None,
) -> dict[str, Any]:
    clients: list[Any] = []
    accounts: list[Account] = []
    cards: list[Card] = []
    if entity_type == "client":
        clients = items or []
        client_ids = [item.id for item in clients]
        accounts = await AccountService(session).list_for_client_ids(client_ids)
        cards = await CardService(session).list_for_client_ids(client_ids)
    elif entity_type == "account":
        accounts = cast(list[Account], items or [])
        clients = await ClientService(session).get_many([item.client_id for item in accounts])
        cards = await CardService(session).list_for_account_ids([item.id for item in accounts])
    else:
        cards = cast(list[Card], items or [])
        accounts = await AccountService(session).get_many([item.account_id for item in cards])
        clients = await ClientService(session).get_many([item.client_id for item in accounts])
    accounts_by_client: dict[str, list[Account]] = {}
    cards_by_account: dict[str, list[Card]] = {}
    cards_by_client: dict[str, list[Card]] = {}
    for account in accounts:
        accounts_by_client.setdefault(str(account.client_id), []).append(account)
    for card in cards:
        cards_by_account.setdefault(str(card.account_id), []).append(card)
    account_client = {str(account.id): str(account.client_id) for account in accounts}
    for card in cards:
        client_id = account_client.get(str(card.account_id))
        if client_id:
            cards_by_client.setdefault(client_id, []).append(card)
    return {
        "clients_by_id": {str(item.id): item for item in clients},
        "accounts_by_id": {str(item.id): item for item in accounts},
        "cards_by_id": {str(item.id): item for item in cards},
        "accounts_by_client": accounts_by_client,
        "cards_by_account": cards_by_account,
        "cards_by_client": cards_by_client,
        "labels": {
            "client": {str(item.id): entity_label("client", item) for item in clients},
            "account": {str(item.id): entity_label("account", item) for item in accounts},
            "card": {str(item.id): entity_label("card", item) for item in cards},
        },
    }


def _filter_values(request: Request) -> dict[str, str]:
    return {
        key.removeprefix("filter_"): value
        for key, value in request.query_params.items()
        if key.startswith("filter_") and value
    }


async def build_entity_list_context(
    session: AsyncSession,
    entity_type: str,
    request: Request | None = None,
    *,
    search: str = "",
    sort: str = "created_at",
    direction: str = "desc",
) -> dict[str, Any]:
    check_entity_type(entity_type)
    schema = await _schema(session, entity_type)
    filters = _filter_values(request) if request is not None else {}
    attr_names = {field.name for field in schema}
    page = _page_param(request)
    offset = (page - 1) * PAGE_SIZE
    items, items_total = await _service(session, entity_type).list_page(
        search=search,
        filters=filters,
        sort=sort,
        direction=direction,
        attr_names=attr_names,
        limit=PAGE_SIZE,
        offset=offset,
    )
    pages = max(1, (items_total + PAGE_SIZE - 1) // PAGE_SIZE)
    return {
        "active": "data",
        "entity_type": entity_type,
        "title": ENTITY_TITLES[entity_type],
        "icon": ENTITY_ICONS[entity_type],
        "schema": schema,
        "items": items,
        "items_total": items_total,
        "relation_columns": RELATION_COLUMNS[entity_type],
        "search": search,
        "sort": sort,
        "direction": direction,
        "filters": filters,
        "page": page,
        "page_size": PAGE_SIZE,
        "pages": pages,
        "has_prev": page > 1,
        "has_next": page < pages,
        **await _relations(session, entity_type, items=items),
    }


async def build_entity_form_context(
    session: AsyncSession,
    entity_type: str,
    *,
    entity_id: uuid.UUID | None,
) -> dict[str, Any]:
    check_entity_type(entity_type)
    entity = await _get_item(session, entity_type, entity_id) if entity_id else None
    schema = await _schema(session, entity_type)
    parent_options: list[Any] = []
    labels: dict[str, dict[str, str]] = {"client": {}, "account": {}, "card": {}}
    if entity_type == "account":
        parent_options = await ClientService(session).list_all()
        labels["client"] = {str(item.id): entity_label("client", item) for item in parent_options}
    elif entity_type == "card":
        parent_options = await AccountService(session).list_all()
        labels["account"] = {str(item.id): entity_label("account", item) for item in parent_options}
    return {
        "active": "data",
        "entity_type": entity_type,
        "title": ENTITY_TITLES[entity_type] if entity_id else f"{ENTITY_TITLES[entity_type]}: новая запись",
        "icon": ENTITY_ICONS[entity_type],
        "entity_id": str(entity_id) if entity_id else None,
        "entity": entity,
        "schema": schema,
        "parent_options": parent_options,
        "labels": labels,
    }


def _tags_from_form(form: Any) -> list[str]:
    tags: list[str] = []
    for raw in form.getlist("tags"):
        if isinstance(raw, str) and raw.strip() and raw.strip() not in tags:
            tags.append(raw.strip())
    return tags


async def _entity_payload(
    entity_type: str,
    request: Request,
    session: AsyncSession,
    *,
    is_update: bool,
) -> ClientCreate | ClientUpdate | AccountCreate | AccountUpdate | CardCreate | CardUpdate:
    form = await request.form()
    schema = await _schema(session, entity_type)
    base = {
        "description": form_str(form, "description"),
        "tags": _tags_from_form(form),
        "attributes": read_entity_attributes(form, schema),
    }
    if entity_type == "client":
        return ClientUpdate(**base) if is_update else ClientCreate(**base)
    if entity_type == "account":
        data = {**base, "client_id": uuid.UUID(form_str(form, "client_id"))}
        return AccountUpdate(**data) if is_update else AccountCreate(**data)
    data = {**base, "account_id": uuid.UUID(form_str(form, "account_id"))}
    return CardUpdate(**data) if is_update else CardCreate(**data)


async def _create_entity(
    session: AsyncSession,
    entity_type: str,
    data: ClientCreate | AccountCreate | CardCreate,
) -> Any:
    return await _service(session, entity_type).create(data)


async def _update_entity(
    session: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    data: ClientUpdate | AccountUpdate | CardUpdate,
) -> Any:
    return await _service(session, entity_type).update(entity_id, data)


async def _delete_entity(session: AsyncSession, entity_type: str, entity_id: uuid.UUID) -> None:
    await _service(session, entity_type).delete(entity_id)


@router.get("/entities-htmx/{entity_type}/table")
async def htmx_table(
    entity_type: str,
    request: Request,
    search: str = "",
    sort: str = "created_at",
    direction: str = "desc",
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    context = await build_entity_list_context(
        session, entity_type, request, search=search, sort=sort, direction=direction
    )
    context["oob_meta"] = True
    return templates.TemplateResponse(request, "partials/entities_table.html", context)


@router.get("/entities-htmx/{entity_type}/{entity_id}/detail")
async def htmx_detail(
    entity_type: str,
    entity_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    check_entity_type(entity_type)
    schema = await _schema(session, entity_type)
    item = await _get_item(session, entity_type, entity_id)
    return templates.TemplateResponse(
        request,
        "partials/entity_detail.html",
        {
            "entity_type": entity_type,
            "item": item,
            "schema": schema,
            **await _relations(session, entity_type, items=[item]),
        },
    )


@router.post("/entities-htmx/{entity_type}")
async def htmx_create(
    entity_type: str,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    check_entity_type(entity_type)
    try:
        data = await _entity_payload(entity_type, request, session, is_update=False)
        created = await commit_and_refresh(
            session,
            await _create_entity(session, entity_type, cast(ClientCreate | AccountCreate | CardCreate, data)),
        )
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            return validation_errors_response(request, templates, exc)
        return form_errors_response(request, templates, "Проверьте связанные сущности")
    except DomainError as exc:
        return form_errors_response(
            request, templates, exc.message, details=exc.details, status_code=exc.status_code
        )
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": f"/templater/{entity_type}s/{created.id}/edit?saved=1",
        },
    )


@router.put("/entities-htmx/{entity_type}/{entity_id}")
async def htmx_update(
    entity_type: str,
    entity_id: uuid.UUID,
    request: Request,
    templates: Jinja2Templates = TemplatesDep,
    session: AsyncSession = SessionDep,
) -> Response:
    check_entity_type(entity_type)
    try:
        data = await _entity_payload(entity_type, request, session, is_update=True)
        await commit_and_refresh(
            session,
            await _update_entity(
                session,
                entity_type,
                entity_id,
                cast(ClientUpdate | AccountUpdate | CardUpdate, data),
            ),
        )
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            return validation_errors_response(request, templates, exc)
        return form_errors_response(request, templates, "Проверьте связанные сущности")
    except DomainError as exc:
        return form_errors_response(
            request, templates, exc.message, details=exc.details, status_code=exc.status_code
        )
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": f"/templater/{entity_type}s/{entity_id}/edit?saved=1",
        },
    )


@router.delete("/entities-htmx/{entity_type}/{entity_id}")
async def htmx_delete(
    entity_type: str,
    entity_id: uuid.UUID,
    redirect: bool = Query(False),
    session: AsyncSession = SessionDep,
) -> Response:
    check_entity_type(entity_type)
    try:
        await _delete_entity(session, entity_type, entity_id)
        await commit_or_409(session, message="Не удалось удалить запись — есть связанные данные")
    except DomainError as exc:
        # Статус 200 (а не exc.status_code): htmx 2.0 по умолчанию не обрабатывает
        # HX-Trigger на 4xx-ответах, поэтому тост не показался бы. Та же конвенция,
        # что и в templates_reg.py.
        return Response(
            status_code=200,
            headers={"HX-Trigger": toast_header(exc.message, toast_type="error")},
        )
    headers = {"HX-Trigger": toast_header("Удалено", refresh_entities=True, close_drawer=True)}
    if redirect:
        headers["HX-Redirect"] = f"/templater/{entity_type}s"
    return Response(status_code=204, headers=headers)


@router.post("/entities-htmx/{entity_type}/export")
async def htmx_export(
    entity_type: str,
    request: Request,
    session: AsyncSession = SessionDep,
) -> StreamingResponse:
    check_entity_type(entity_type)
    form = await request.form()
    ids = [uuid.UUID(str(raw)) for raw in form.getlist("ids") if raw]
    req = ExportRequest(
        clients=ids if entity_type == "client" else [],
        accounts=ids if entity_type == "account" else [],
        cards=ids if entity_type == "card" else [],
    )
    package = await ExportImportService(session).export(req)
    payload = json.dumps(package.model_dump(), ensure_ascii=False, indent=2, default=str).encode("utf-8")

    def stream() -> Iterator[bytes]:
        yield payload

    return StreamingResponse(
        stream(),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="tm-export-{entity_type}.json"'},
    )
