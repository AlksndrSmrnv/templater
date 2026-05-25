from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, Card, Client


class ClientRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[Client]:
        stmt = select(Client).order_by(Client.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

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
