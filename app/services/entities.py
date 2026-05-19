from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
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

    async def list(self) -> list[Client]:
        return await self.repo.list()

    async def get(self, client_id: uuid.UUID) -> Client:
        c = await self.repo.get(client_id)
        if c is None:
            raise NotFoundError("Клиент не найден")
        return c

    async def create(self, data: ClientCreate) -> Client:
        attrs = await self.schema.validate_attributes("client", data.attributes)
        client = Client(
            description=data.description,
            tags=list(data.tags),
            attributes=attrs,
        )
        await self.repo.add(client)
        await self.session.commit()
        await self.session.refresh(client)
        return client

    async def update(self, client_id: uuid.UUID, data: ClientUpdate) -> Client:
        client = await self.get(client_id)
        client.description = data.description
        client.tags = list(data.tags)
        client.attributes = await self.schema.validate_attributes("client", data.attributes)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(client)
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
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise IntegrityViolation("Не удалось удалить клиента — есть связанные данные") from exc


class AccountService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = AccountRepository(session)
        self.clients = ClientRepository(session)
        self.schema = AttributeSchemaService(session)

    async def list(self, *, client_id: uuid.UUID | None = None) -> list[Account]:
        return await self.repo.list(client_id=client_id)

    async def get(self, account_id: uuid.UUID) -> Account:
        a = await self.repo.get(account_id)
        if a is None:
            raise NotFoundError("Счёт не найден")
        return a

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
        await self.session.commit()
        await self.session.refresh(account)
        return account

    async def update(self, account_id: uuid.UUID, data: AccountUpdate) -> Account:
        account = await self.get(account_id)
        client = await self.clients.get(data.client_id)
        if client is None:
            raise NotFoundError("Клиент не найден")
        account.client_id = data.client_id
        account.description = data.description
        account.tags = list(data.tags)
        account.attributes = await self.schema.validate_attributes("account", data.attributes)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(account)
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
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise IntegrityViolation("Не удалось удалить счёт — есть связанные данные") from exc


class CardService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = CardRepository(session)
        self.accounts = AccountRepository(session)
        self.schema = AttributeSchemaService(session)

    async def list(self, *, account_id: uuid.UUID | None = None) -> list[Card]:
        return await self.repo.list(account_id=account_id)

    async def get(self, card_id: uuid.UUID) -> Card:
        c = await self.repo.get(card_id)
        if c is None:
            raise NotFoundError("Карта не найдена")
        return c

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
        await self.session.commit()
        await self.session.refresh(card)
        return card

    async def update(self, card_id: uuid.UUID, data: CardUpdate) -> Card:
        card = await self.get(card_id)
        account = await self.accounts.get(data.account_id)
        if account is None:
            raise NotFoundError("Счёт не найден")
        card.account_id = data.account_id
        card.description = data.description
        card.tags = list(data.tags)
        card.attributes = await self.schema.validate_attributes("card", data.attributes)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(card)
        return card

    async def delete(self, card_id: uuid.UUID) -> None:
        card = await self.get(card_id)
        await self.repo.delete(card)
        await self.session.commit()
