from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Account, Card, Client
from app.repositories.entity import (
    AccountRepository,
    CardRepository,
    ClientRepository,
)
from app.schemas.entity import (
    AccountCreate,
    AccountUpdate,
    CardCreate,
    CardUpdate,
    ClientCreate,
    ClientUpdate,
)
from app.services.attribute_schema import AttributeSchemaService
from app.utils.errors import IntegrityViolation, NotFoundError


class ClientService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = ClientRepository(session)
        self.schema = AttributeSchemaService(session)

    async def list_all(self) -> list[Client]:
        return await self.repo.list_all()

    async def get(self, client_id: uuid.UUID) -> Client:
        c = await self.repo.get(client_id)
        if c is None:
            raise NotFoundError("Клиент не найден")
        return c

    async def get_many(self, ids: Sequence[uuid.UUID]) -> list[Client]:
        return await self.repo.get_many(ids)

    async def create(self, data: ClientCreate) -> Client:
        attrs = await self.schema.validate_attributes("client", data.attributes)
        client = Client(
            description=data.description,
            tags=list(data.tags),
            attributes=attrs,
        )
        await self.repo.add(client)
        return client

    async def update(self, client_id: uuid.UUID, data: ClientUpdate) -> Client:
        client = await self.get(client_id)
        client.description = data.description
        client.tags = list(data.tags)
        client.attributes = await self.schema.validate_attributes(
            "client", data.attributes, preserve_existing=client.attributes
        )
        await self.session.flush()
        return client

    async def delete(self, client_id: uuid.UUID) -> None:
        client = await self.get(client_id)
        n = await self.repo.count_accounts(client_id)
        if n > 0:
            raise IntegrityViolation(
                f"К клиенту привязано счетов: {n}. Удалите их сначала.",
                details={"dependent_accounts": n},
        )
        await self.repo.delete(client)


class AccountService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = AccountRepository(session)
        self.clients = ClientRepository(session)
        self.schema = AttributeSchemaService(session)

    async def list_all(self, *, client_id: uuid.UUID | None = None) -> list[Account]:
        return await self.repo.list_all(client_id=client_id)

    async def get(self, account_id: uuid.UUID) -> Account:
        a = await self.repo.get(account_id)
        if a is None:
            raise NotFoundError("Счёт не найден")
        return a

    async def get_many(self, ids: Sequence[uuid.UUID]) -> list[Account]:
        return await self.repo.get_many(ids)

    async def list_for_client_ids(self, client_ids: Sequence[uuid.UUID]) -> list[Account]:
        return await self.repo.list_for_client_ids(client_ids)

    async def create(self, data: AccountCreate) -> Account:
        client = await self.clients.get(data.client_id)
        if client is None:
            raise NotFoundError("Клиент не найден")
        attrs = await self.schema.validate_attributes("account", data.attributes)
        account = Account(
            client_id=data.client_id,
            description=data.description,
            tags=list(data.tags),
            attributes=attrs,
        )
        await self.repo.add(account)
        return account

    async def update(self, account_id: uuid.UUID, data: AccountUpdate) -> Account:
        account = await self.get(account_id)
        client = await self.clients.get(data.client_id)
        if client is None:
            raise NotFoundError("Клиент не найден")
        account.client_id = data.client_id
        account.description = data.description
        account.tags = list(data.tags)
        account.attributes = await self.schema.validate_attributes(
            "account", data.attributes, preserve_existing=account.attributes
        )
        await self.session.flush()
        return account

    async def delete(self, account_id: uuid.UUID) -> None:
        account = await self.get(account_id)
        n = await self.repo.count_cards(account_id)
        if n > 0:
            raise IntegrityViolation(
                f"К счёту привязано карт: {n}. Удалите их сначала.",
                details={"dependent_cards": n},
        )
        await self.repo.delete(account)


class CardService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = CardRepository(session)
        self.accounts = AccountRepository(session)
        self.schema = AttributeSchemaService(session)

    async def list_all(
        self,
        *,
        account_id: uuid.UUID | None = None,
        client_id: uuid.UUID | None = None,
    ) -> list[Card]:
        return await self.repo.list_all(account_id=account_id, client_id=client_id)

    async def get(self, card_id: uuid.UUID) -> Card:
        c = await self.repo.get(card_id)
        if c is None:
            raise NotFoundError("Карта не найдена")
        return c

    async def get_many(self, ids: Sequence[uuid.UUID]) -> list[Card]:
        return await self.repo.get_many(ids)

    async def list_for_account_ids(self, account_ids: Sequence[uuid.UUID]) -> list[Card]:
        return await self.repo.list_for_account_ids(account_ids)

    async def list_for_client_ids(self, client_ids: Sequence[uuid.UUID]) -> list[Card]:
        return await self.repo.list_for_client_ids(client_ids)

    async def create(self, data: CardCreate) -> Card:
        account = await self.accounts.get(data.account_id)
        if account is None:
            raise NotFoundError("Счёт не найден")
        attrs = await self.schema.validate_attributes("card", data.attributes)
        card = Card(
            account_id=data.account_id,
            description=data.description,
            tags=list(data.tags),
            attributes=attrs,
        )
        await self.repo.add(card)
        return card

    async def update(self, card_id: uuid.UUID, data: CardUpdate) -> Card:
        card = await self.get(card_id)
        account = await self.accounts.get(data.account_id)
        if account is None:
            raise NotFoundError("Счёт не найден")
        card.account_id = data.account_id
        card.description = data.description
        card.tags = list(data.tags)
        card.attributes = await self.schema.validate_attributes(
            "card", data.attributes, preserve_existing=card.attributes
        )
        await self.session.flush()
        return card

    async def delete(self, card_id: uuid.UUID) -> None:
        card = await self.get(card_id)
        await self.repo.delete(card)
