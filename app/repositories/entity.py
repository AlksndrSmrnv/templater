from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import Text, func, or_, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, Card, Client, MessageTemplate, ReferenceValue

# Real columns (not JSONB attributes) the entity list may sort on.
_REAL_SORT_COLUMNS = frozenset({"description", "created_at", "updated_at"})


def _entity_filter_conditions(
    model: Any,
    *,
    search: str,
    filters: dict[str, str],
    attr_names: set[str],
) -> list[Any]:
    """WHERE conditions for the entity list: a free-text search across
    description / attributes / tags, plus per-attribute substring filters.

    The attributes search casts the JSONB column to text (preserves UTF-8, covers
    keys and values) — the SQL equivalent of the previous ``json.dumps`` sweep.
    """

    conditions: list[Any] = []
    needle = search.strip()
    if needle:
        like = f"%{needle}%"
        conditions.append(
            or_(
                model.description.ilike(like),
                sa_cast(model.attributes, Text).ilike(like),
                func.array_to_string(model.tags, " ").ilike(like),
            )
        )
    for name, value in filters.items():
        text = value.strip()
        if not text or name not in attr_names:
            continue
        conditions.append(model.attributes[name].astext.ilike(f"%{text}%"))
    return conditions


def _entity_order_by(
    model: Any,
    *,
    sort: str,
    direction: str,
    attr_names: set[str],
) -> Any:
    if sort in _REAL_SORT_COLUMNS:
        col = getattr(model, sort)
    elif sort == "tags":
        col = func.array_to_string(model.tags, " ")
    elif sort in attr_names:
        col = model.attributes[sort].astext
    else:
        col = model.created_at
    return col.desc() if direction == "desc" else col.asc()


async def _query_entity_page(
    session: AsyncSession,
    model: Any,
    *,
    search: str,
    filters: dict[str, str],
    sort: str,
    direction: str,
    attr_names: set[str],
    limit: int,
    offset: int,
) -> tuple[list[Any], int]:
    """Return ``(page_rows, total_matching)`` for the entity list, doing search,
    filtering, sorting and pagination in SQL (bounded result set, no Python sweep)."""

    conditions = _entity_filter_conditions(
        model, search=search, filters=filters, attr_names=attr_names
    )
    count_stmt = select(func.count()).select_from(model)
    page_stmt = select(model)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
        page_stmt = page_stmt.where(*conditions)
    total = int((await session.execute(count_stmt)).scalar_one())
    order = _entity_order_by(model, sort=sort, direction=direction, attr_names=attr_names)
    # ``model.id`` is a stable tiebreaker so paging is deterministic.
    page_stmt = page_stmt.order_by(order, model.id).limit(limit).offset(offset)
    rows = list((await session.execute(page_stmt)).scalars().all())
    return rows, total


class ClientRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[Client]:
        stmt = select(Client).order_by(Client.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_page(
        self,
        *,
        search: str,
        filters: dict[str, str],
        sort: str,
        direction: str,
        attr_names: set[str],
        limit: int,
        offset: int,
    ) -> tuple[list[Client], int]:
        return await _query_entity_page(
            self.session, Client, search=search, filters=filters, sort=sort,
            direction=direction, attr_names=attr_names, limit=limit, offset=offset,
        )

    async def get(self, client_id: uuid.UUID) -> Client | None:
        return await self.session.get(Client, client_id)

    async def get_many(self, ids: Sequence[uuid.UUID]) -> list[Client]:
        if not ids:
            return []
        stmt = select(Client).where(Client.id.in_(ids))
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_accounts(self, client_id: uuid.UUID) -> int:
        stmt = select(func.count(Account.id)).where(Account.client_id == client_id)
        return int((await self.session.execute(stmt)).scalar_one())

    async def add(self, client: Client) -> Client:
        self.session.add(client)
        await self.session.flush()
        return client

    async def delete(self, client: Client) -> None:
        await self.session.delete(client)


class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self, *, client_id: uuid.UUID | None = None) -> list[Account]:
        stmt = select(Account).order_by(Account.created_at.desc())
        if client_id is not None:
            stmt = stmt.where(Account.client_id == client_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_page(
        self,
        *,
        search: str,
        filters: dict[str, str],
        sort: str,
        direction: str,
        attr_names: set[str],
        limit: int,
        offset: int,
    ) -> tuple[list[Account], int]:
        return await _query_entity_page(
            self.session, Account, search=search, filters=filters, sort=sort,
            direction=direction, attr_names=attr_names, limit=limit, offset=offset,
        )

    async def get(self, account_id: uuid.UUID) -> Account | None:
        return await self.session.get(Account, account_id)

    async def get_many(self, ids: Sequence[uuid.UUID]) -> list[Account]:
        if not ids:
            return []
        stmt = select(Account).where(Account.id.in_(ids))
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_client_ids(self, client_ids: Sequence[uuid.UUID]) -> list[Account]:
        if not client_ids:
            return []
        stmt = (
            select(Account)
            .where(Account.client_id.in_(client_ids))
            .order_by(Account.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_cards(self, account_id: uuid.UUID) -> int:
        stmt = select(func.count(Card.id)).where(Card.account_id == account_id)
        return int((await self.session.execute(stmt)).scalar_one())

    async def add(self, account: Account) -> Account:
        self.session.add(account)
        await self.session.flush()
        return account

    async def delete(self, account: Account) -> None:
        await self.session.delete(account)


class CardRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(
        self,
        *,
        account_id: uuid.UUID | None = None,
        client_id: uuid.UUID | None = None,
    ) -> list[Card]:
        stmt = select(Card).order_by(Card.created_at.desc())
        if client_id is not None:
            stmt = stmt.join(Account, Card.account_id == Account.id).where(Account.client_id == client_id)
        if account_id is not None:
            stmt = stmt.where(Card.account_id == account_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_page(
        self,
        *,
        search: str,
        filters: dict[str, str],
        sort: str,
        direction: str,
        attr_names: set[str],
        limit: int,
        offset: int,
    ) -> tuple[list[Card], int]:
        return await _query_entity_page(
            self.session, Card, search=search, filters=filters, sort=sort,
            direction=direction, attr_names=attr_names, limit=limit, offset=offset,
        )

    async def get(self, card_id: uuid.UUID) -> Card | None:
        return await self.session.get(Card, card_id)

    async def get_many(self, ids: Sequence[uuid.UUID]) -> list[Card]:
        if not ids:
            return []
        stmt = select(Card).where(Card.id.in_(ids))
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_account_ids(self, account_ids: Sequence[uuid.UUID]) -> list[Card]:
        if not account_ids:
            return []
        stmt = (
            select(Card)
            .where(Card.account_id.in_(account_ids))
            .order_by(Card.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_client_ids(self, client_ids: Sequence[uuid.UUID]) -> list[Card]:
        if not client_ids:
            return []
        stmt = (
            select(Card)
            .join(Account, Card.account_id == Account.id)
            .where(Account.client_id.in_(client_ids))
            .order_by(Card.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def add(self, card: Card) -> Card:
        self.session.add(card)
        await self.session.flush()
        return card

    async def delete(self, card: Card) -> None:
        await self.session.delete(card)


async def count_attribute_usage(
    session: AsyncSession,
    attrs: Sequence[Any],
) -> dict[uuid.UUID, dict[str, int]]:
    """Count, for each attribute definition in ``attrs``, where it is currently used.

    Returns a map ``attr.id -> {"records": int, "templates": int}``:

    - ``records``: entity rows holding a (non-empty) value under ``attr.name`` in their JSONB
      ``attributes``. Data entities map to Client/Account/Card; reference types live in
      ``reference_values`` filtered by ``entity_type``.
    - ``templates``: message templates whose content mentions ``attr.name`` — a substring
      heuristic, so the figure is approximate and only meant as a deletion warning.

    Counts are batched (one aggregate query per entity type plus one query for all template
    contents) to avoid an N+1 over the attribute list on the settings page.
    """

    result: dict[uuid.UUID, dict[str, int]] = {a.id: {"records": 0, "templates": 0} for a in attrs}
    if not attrs:
        return result

    data_models: dict[str, Any] = {"client": Client, "account": Account, "card": Card}
    by_type: dict[str, list[Any]] = defaultdict(list)
    for a in attrs:
        by_type[a.entity_type].append(a)

    # Records: one query per entity type using count(*) FILTER (WHERE attributes ? name).
    for entity_type, group in by_type.items():
        model = cast(Any, data_models.get(entity_type)) or ReferenceValue
        cols = [
            func.count().filter(model.attributes.has_key(a.name)).label(f"c{i}")
            for i, a in enumerate(group)
        ]
        stmt = select(*cols).select_from(model)
        if data_models.get(entity_type) is None:
            stmt = stmt.where(ReferenceValue.entity_type == entity_type)
        row = (await session.execute(stmt)).one()._mapping
        for i, a in enumerate(group):
            result[a.id]["records"] = int(row[f"c{i}"])

    # Templates: fetch all contents once and tally substring matches in Python.
    contents = list((await session.execute(select(MessageTemplate.content))).scalars().all())
    for a in attrs:
        result[a.id]["templates"] = sum(1 for content in contents if a.name in content)

    return result


async def find_entities_referencing(
    session: AsyncSession,
    *,
    ref_entity_type: str,
    target_id: uuid.UUID,
    attribute_names_by_entity: dict[str, list[str]],
) -> dict[str, int]:
    """For each owner entity_type → list of attribute names that reference ``ref_entity_type``,
    count how many entity rows hold ``target_id`` in any of those attributes.
    """

    result: dict[str, int] = {}
    table_map: dict[str, Any] = {"client": Client, "account": Account, "card": Card}
    target_str = str(target_id)
    for owner, attrs in attribute_names_by_entity.items():
        model = cast(Any, table_map.get(owner))
        if model is None or not attrs:
            continue
        conditions = [model.attributes[name].astext == target_str for name in attrs]
        stmt = select(func.count(model.id)).where(or_(*conditions))
        count = int((await session.execute(stmt)).scalar_one())
        if count:
            result[owner] = count
    return result
