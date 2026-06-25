from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import Text, func, or_, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Account,
    Card,
    Client,
    FilledTemplate,
    MessageTemplate,
    RequestChain,
)

# Real columns (not JSONB attributes) the entity list may sort on.
_REAL_SORT_COLUMNS = frozenset({"description", "created_at", "updated_at"})
# Escape char for LIKE patterns (a single backslash).
_LIKE_ESCAPE = "\\"


def _like_escape(text: str) -> str:
    """Escape LIKE metacharacters so user input matches literally — parity with the
    previous Python substring search, where ``%``/``_`` had no special meaning.
    Paired with ``escape=_LIKE_ESCAPE`` on the ``ilike()`` calls.
    """

    return (
        text.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )


def group_visibility_condition(
    model: Any, visible_group_ids: set[uuid.UUID] | None
) -> Any:
    """WHERE condition restricting rows to those an unlocked-group set may see:
    public rows (``group_id IS NULL``) plus rows in an unlocked group.

    Group membership lives only on :class:`Client`; accounts and cards inherit
    their parent client's group via a correlated ``EXISTS`` (relationship
    ``.has()``), so there is a single source of truth and nothing to keep in
    sync. ``visible_group_ids=None`` means "no restriction" (internal/admin
    callers); a set — possibly empty — restricts to public + that set.
    """

    if visible_group_ids is None:
        return None
    # Models with their own ``group_id`` column (Client, FilledTemplate,
    # RequestChain): public rows plus rows in an unlocked group.
    if model is Client or model is FilledTemplate or model is RequestChain:
        column = model.group_id
        cond: Any = column.is_(None)
        if visible_group_ids:
            cond = or_(cond, column.in_(visible_group_ids))
        return cond
    if model is Account:
        return Account.client.has(group_visibility_condition(Client, visible_group_ids))
    if model is Card:
        return Card.account.has(
            Account.client.has(group_visibility_condition(Client, visible_group_ids))
        )
    return None


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
        like = f"%{_like_escape(needle)}%"
        conditions.append(
            or_(
                model.description.ilike(like, escape=_LIKE_ESCAPE),
                sa_cast(model.attributes, Text).ilike(like, escape=_LIKE_ESCAPE),
                func.array_to_string(model.tags, " ").ilike(like, escape=_LIKE_ESCAPE),
            )
        )
    for name, value in filters.items():
        text = value.strip()
        if not text or name not in attr_names:
            continue
        conditions.append(
            model.attributes[name].astext.ilike(f"%{_like_escape(text)}%", escape=_LIKE_ESCAPE)
        )
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
        # ``->>`` is NULL for a missing key; coalesce to '' so rows lacking the
        # attribute sort like the empty string did under the old Python sort
        # instead of NULLs jumping to the top under DESC.
        col = func.coalesce(model.attributes[sort].astext, "")
    else:
        col = model.created_at
    return col.desc() if direction == "desc" else col.asc()


async def _entity_position(
    session: AsyncSession,
    model: Any,
    target_id: uuid.UUID,
    *,
    visible_group_ids: set[uuid.UUID] | None,
) -> int | None:
    """0-based index of ``target_id`` under the list's default order
    (``created_at DESC, id ASC``), restricted to rows the caller may see.

    Used by the ``?open=<id>`` deep link to compute which page to show so the
    target row is always rendered (and highlighted) regardless of pagination.
    Returns ``None`` when the target is missing or outside the visible groups —
    callers fall back to the first page instead of erroring.
    """

    cond = group_visibility_condition(model, visible_group_ids)
    target_stmt = select(model.created_at, model.id).where(model.id == target_id)
    if cond is not None:
        target_stmt = target_stmt.where(cond)
    target = (await session.execute(target_stmt)).one_or_none()
    if target is None:
        return None
    before = or_(
        model.created_at > target.created_at,
        (model.created_at == target.created_at) & (model.id < target.id),
    )
    count_stmt = select(func.count()).select_from(model).where(before)
    if cond is not None:
        count_stmt = count_stmt.where(cond)
    return int((await session.execute(count_stmt)).scalar_one())


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
    extra_conditions: list[Any] | None = None,
) -> tuple[list[Any], int]:
    """Return ``(page_rows, total_matching)`` for the entity list, doing search,
    filtering, sorting and pagination in SQL (bounded result set, no Python sweep).

    ``extra_conditions`` are ANDed in alongside the search/filter predicates —
    used to enforce access-group visibility."""

    conditions = _entity_filter_conditions(
        model, search=search, filters=filters, attr_names=attr_names
    )
    if extra_conditions:
        conditions.extend(extra_conditions)
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

    async def list_all(
        self, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> list[Client]:
        stmt = select(Client).order_by(Client.created_at.desc())
        cond = group_visibility_condition(Client, visible_group_ids)
        if cond is not None:
            stmt = stmt.where(cond)
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
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> tuple[list[Client], int]:
        cond = group_visibility_condition(Client, visible_group_ids)
        return await _query_entity_page(
            self.session, Client, search=search, filters=filters, sort=sort,
            direction=direction, attr_names=attr_names, limit=limit, offset=offset,
            extra_conditions=[cond] if cond is not None else None,
        )

    async def get(
        self, client_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> Client | None:
        if visible_group_ids is None:
            return await self.session.get(Client, client_id)
        stmt = select(Client).where(
            Client.id == client_id, group_visibility_condition(Client, visible_group_ids)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_many(
        self, ids: Sequence[uuid.UUID], *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> list[Client]:
        if not ids:
            return []
        stmt = select(Client).where(Client.id.in_(ids))
        cond = group_visibility_condition(Client, visible_group_ids)
        if cond is not None:
            stmt = stmt.where(cond)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_accounts(self, client_id: uuid.UUID) -> int:
        stmt = select(func.count(Account.id)).where(Account.client_id == client_id)
        return int((await self.session.execute(stmt)).scalar_one())

    async def position_of(
        self, client_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> int | None:
        return await _entity_position(
            self.session, Client, client_id, visible_group_ids=visible_group_ids
        )

    async def add(self, client: Client) -> Client:
        self.session.add(client)
        await self.session.flush()
        return client

    async def delete(self, client: Client) -> None:
        await self.session.delete(client)


class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(
        self,
        *,
        client_id: uuid.UUID | None = None,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> list[Account]:
        stmt = select(Account).order_by(Account.created_at.desc())
        if client_id is not None:
            stmt = stmt.where(Account.client_id == client_id)
        cond = group_visibility_condition(Account, visible_group_ids)
        if cond is not None:
            stmt = stmt.where(cond)
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
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> tuple[list[Account], int]:
        cond = group_visibility_condition(Account, visible_group_ids)
        return await _query_entity_page(
            self.session, Account, search=search, filters=filters, sort=sort,
            direction=direction, attr_names=attr_names, limit=limit, offset=offset,
            extra_conditions=[cond] if cond is not None else None,
        )

    async def get(
        self, account_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> Account | None:
        if visible_group_ids is None:
            return await self.session.get(Account, account_id)
        stmt = select(Account).where(
            Account.id == account_id, group_visibility_condition(Account, visible_group_ids)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_many(
        self, ids: Sequence[uuid.UUID], *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> list[Account]:
        if not ids:
            return []
        stmt = select(Account).where(Account.id.in_(ids))
        cond = group_visibility_condition(Account, visible_group_ids)
        if cond is not None:
            stmt = stmt.where(cond)
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

    async def position_of(
        self, account_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> int | None:
        return await _entity_position(
            self.session, Account, account_id, visible_group_ids=visible_group_ids
        )

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
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> list[Card]:
        stmt = select(Card).order_by(Card.created_at.desc())
        if client_id is not None:
            stmt = stmt.join(Account, Card.account_id == Account.id).where(Account.client_id == client_id)
        if account_id is not None:
            stmt = stmt.where(Card.account_id == account_id)
        cond = group_visibility_condition(Card, visible_group_ids)
        if cond is not None:
            stmt = stmt.where(cond)
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
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> tuple[list[Card], int]:
        cond = group_visibility_condition(Card, visible_group_ids)
        return await _query_entity_page(
            self.session, Card, search=search, filters=filters, sort=sort,
            direction=direction, attr_names=attr_names, limit=limit, offset=offset,
            extra_conditions=[cond] if cond is not None else None,
        )

    async def get(
        self, card_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> Card | None:
        if visible_group_ids is None:
            return await self.session.get(Card, card_id)
        stmt = select(Card).where(
            Card.id == card_id, group_visibility_condition(Card, visible_group_ids)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_many(
        self, ids: Sequence[uuid.UUID], *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> list[Card]:
        if not ids:
            return []
        stmt = select(Card).where(Card.id.in_(ids))
        cond = group_visibility_condition(Card, visible_group_ids)
        if cond is not None:
            stmt = stmt.where(cond)
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

    async def position_of(
        self, card_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> int | None:
        return await _entity_position(
            self.session, Card, card_id, visible_group_ids=visible_group_ids
        )

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

    - ``records``: entity rows (Client/Account/Card) holding a (non-empty) value under
      ``attr.name`` in their JSONB ``attributes``.
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
        model = cast(Any, data_models.get(entity_type))
        if model is None:
            continue
        cols = [
            func.count().filter(model.attributes.has_key(a.name)).label(f"c{i}")
            for i, a in enumerate(group)
        ]
        stmt = select(*cols).select_from(model)
        row = (await session.execute(stmt)).one()._mapping
        for i, a in enumerate(group):
            result[a.id]["records"] = int(row[f"c{i}"])

    # Templates: fetch all contents once and tally substring matches in Python.
    contents = list((await session.execute(select(MessageTemplate.content))).scalars().all())
    for a in attrs:
        result[a.id]["templates"] = sum(1 for content in contents if a.name in content)

    return result
