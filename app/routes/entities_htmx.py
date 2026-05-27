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
from app.services.references import ReferenceService
from app.utils.errors import DomainError, NotFoundError

router = APIRouter()

ENTITY_TITLES = {
    "client": "👥 Клиенты",
    "account": "🏦 Счета",
    "card": "💳 Карты",
}
RELATION_COLUMNS = {
    "client": ("accounts", "cards"),
    "account": ("client", "cards"),
    "card": ("client", "account"),
}


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


async def _schema(
    session: AsyncSession, entity_type: str, *, include_deprecated: bool = False
) -> list[AttributeDefinition]:
    return await AttributeSchemaService(session).list_schema(
        entity_type, include_deprecated=include_deprecated
    )


async def _list_items(session: AsyncSession, entity_type: str) -> list[Any]:
    if entity_type == "client":
        return await ClientService(session).list_all()
    if entity_type == "account":
        return await AccountService(session).list_all()
    return await CardService(session).list_all()


async def _get_item(session: AsyncSession, entity_type: str, item_id: uuid.UUID) -> Any:
    if entity_type == "client":
        return await ClientService(session).get(item_id)
    if entity_type == "account":
        return await AccountService(session).get(item_id)
    return await CardService(session).get(item_id)


def _collect_ref_entities(schema: list[AttributeDefinition]) -> tuple[str, ...]:
    ref_entities: set[str] = set()
    for field in schema:
        if field.data_type != "ref":
            continue
        ref_entity = (field.options or {}).get("ref_entity")
        if isinstance(ref_entity, str) and ref_entity:
            ref_entities.add(ref_entity)
    return tuple(sorted(ref_entities))


async def _reference_options(
    session: AsyncSession,
    schema: list[AttributeDefinition],
) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for ref_entity in _collect_ref_entities(schema):
        values = await ReferenceService(session).list_by_type(ref_entity)
        out[ref_entity] = {
            str(item.id): item.name + (f" ({item.code})" if item.code else "") for item in values
        }
    return out


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


def _filter_and_sort(
    items: list[Any],
    schema: list[AttributeDefinition],
    *,
    search: str,
    sort: str,
    direction: str,
    filters: dict[str, str],
) -> list[Any]:
    query = search.strip().lower()
    if query:
        items = [
            item
            for item in items
            if query
            in (
                json.dumps(item.attributes or {}, ensure_ascii=False)
                + " "
                + " ".join(item.tags or [])
                + " "
                + (item.description or "")
            ).lower()
        ]
    for name, value in filters.items():
        needle = value.strip().lower()
        if not needle:
            continue
        items = [
            item
            for item in items
            if needle in str((item.attributes or {}).get(name, "")).lower()
        ]

    attr_names = {field.name for field in schema}
    sort_key = sort if sort in {"description", "tags", "created_at", "updated_at"} or sort in attr_names else "created_at"

    def key(item: Any) -> str:
        if sort_key in attr_names:
            return str((item.attributes or {}).get(sort_key, "")).lower()
        if sort_key == "tags":
            return " ".join(item.tags or []).lower()
        return str(getattr(item, sort_key, "") or "").lower()

    return sorted(items, key=key, reverse=direction == "desc")


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
    items_all = await _list_items(session, entity_type)
    items = _filter_and_sort(
        items_all,
        schema,
        search=search,
        sort=sort,
        direction=direction,
        filters=filters,
    )
    return {
        "active": "data",
        "entity_type": entity_type,
        "title": ENTITY_TITLES[entity_type],
        "schema": schema,
        "items": items,
        "items_total": len(items_all),
        "relation_columns": RELATION_COLUMNS[entity_type],
        "search": search,
        "sort": sort,
        "direction": direction,
        "filters": filters,
        "ref_options": await _reference_options(session, schema),
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
    schema = await _schema(session, entity_type, include_deprecated=entity is not None)
    if entity is not None:
        schema = [
            field
            for field in schema
            if not field.is_deprecated or (entity.attributes or {}).get(field.name) is not None
        ]
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
        "entity_id": str(entity_id) if entity_id else None,
        "entity": entity,
        "schema": schema,
        "parent_options": parent_options,
        "ref_options": await _reference_options(session, schema),
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
    include_deprecated: bool,
) -> ClientCreate | ClientUpdate | AccountCreate | AccountUpdate | CardCreate | CardUpdate:
    form = await request.form()
    schema = await _schema(session, entity_type, include_deprecated=include_deprecated)
    base = {
        "description": form_str(form, "description"),
        "tags": _tags_from_form(form),
        "attributes": read_entity_attributes(form, schema),
    }
    if entity_type == "client":
        return ClientUpdate(**base) if include_deprecated else ClientCreate(**base)
    if entity_type == "account":
        data = {**base, "client_id": uuid.UUID(form_str(form, "client_id"))}
        return AccountUpdate(**data) if include_deprecated else AccountCreate(**data)
    data = {**base, "account_id": uuid.UUID(form_str(form, "account_id"))}
    return CardUpdate(**data) if include_deprecated else CardCreate(**data)


async def _create_entity(
    session: AsyncSession,
    entity_type: str,
    data: ClientCreate | AccountCreate | CardCreate,
) -> Any:
    if entity_type == "client":
        return await ClientService(session).create(data)  # type: ignore[arg-type]
    if entity_type == "account":
        return await AccountService(session).create(data)  # type: ignore[arg-type]
    return await CardService(session).create(data)  # type: ignore[arg-type]


async def _update_entity(
    session: AsyncSession,
    entity_type: str,
    entity_id: uuid.UUID,
    data: ClientUpdate | AccountUpdate | CardUpdate,
) -> Any:
    if entity_type == "client":
        return await ClientService(session).update(entity_id, data)  # type: ignore[arg-type]
    if entity_type == "account":
        return await AccountService(session).update(entity_id, data)  # type: ignore[arg-type]
    return await CardService(session).update(entity_id, data)  # type: ignore[arg-type]


async def _delete_entity(session: AsyncSession, entity_type: str, entity_id: uuid.UUID) -> None:
    if entity_type == "client":
        await ClientService(session).delete(entity_id)
    elif entity_type == "account":
        await AccountService(session).delete(entity_id)
    else:
        await CardService(session).delete(entity_id)


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
            "ref_options": await _reference_options(session, schema),
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
        data = await _entity_payload(entity_type, request, session, include_deprecated=False)
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
        data = await _entity_payload(entity_type, request, session, include_deprecated=True)
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
    await _delete_entity(session, entity_type, entity_id)
    await commit_or_409(session, message="Не удалось удалить запись — есть связанные данные")
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
