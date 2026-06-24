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
from app.utils.errors import IntegrityViolation, NotFoundError, ValidationFailed


def _resolve_group_id(
    group_id: uuid.UUID | None, allowed_group_ids: set[uuid.UUID] | None
) -> uuid.UUID | None:
    """Validate a chosen access group before writing it onto a client.

    ``None`` means public. A non-null group must be one the caller has unlocked
    (``allowed_group_ids``) — you can't stash data in a group you don't control.
    ``allowed_group_ids=None`` disables the check for internal/admin callers.
    """

    if group_id is None:
        return None
    if allowed_group_ids is not None and group_id not in allowed_group_ids:
        raise ValidationFailed(
            "Нельзя поместить данные в недоступную группу — сначала разблокируйте её паролем"
        )
    return group_id


class ClientService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = ClientRepository(session)
        self.schema = AttributeSchemaService(session)

    async def list_all(
        self, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> list[Client]:
        return await self.repo.list_all(visible_group_ids=visible_group_ids)

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
        return await self.repo.list_page(
            search=search, filters=filters, sort=sort, direction=direction,
            attr_names=attr_names, limit=limit, offset=offset,
            visible_group_ids=visible_group_ids,
        )

    async def get(
        self, client_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> Client:
        c = await self.repo.get(client_id, visible_group_ids=visible_group_ids)
        if c is None:
            raise NotFoundError("Клиент не найден")
        return c

    async def get_many(
        self, ids: Sequence[uuid.UUID], *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> list[Client]:
        return await self.repo.get_many(ids, visible_group_ids=visible_group_ids)

    async def position_of(
        self, client_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> int | None:
        return await self.repo.position_of(client_id, visible_group_ids=visible_group_ids)

    async def create(
        self, data: ClientCreate, *, allowed_group_ids: set[uuid.UUID] | None = None
    ) -> Client:
        attrs = await self.schema.validate_attributes("client", data.attributes)
        client = Client(
            description=data.description,
            tags=list(data.tags),
            attributes=attrs,
            group_id=_resolve_group_id(data.group_id, allowed_group_ids),
        )
        await self.repo.add(client)
        return client

    async def update(
        self,
        client_id: uuid.UUID,
        data: ClientUpdate,
        *,
        allowed_group_ids: set[uuid.UUID] | None = None,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> Client:
        client = await self.get(client_id, visible_group_ids=visible_group_ids)
        client.description = data.description
        client.tags = list(data.tags)
        client.group_id = _resolve_group_id(data.group_id, allowed_group_ids)
        client.attributes = await self.schema.validate_attributes(
            "client", data.attributes, preserve_existing=client.attributes
        )
        await self.session.flush()
        return client

    async def delete(
        self, client_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> None:
        client = await self.get(client_id, visible_group_ids=visible_group_ids)
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

    async def list_all(
        self,
        *,
        client_id: uuid.UUID | None = None,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> list[Account]:
        return await self.repo.list_all(client_id=client_id, visible_group_ids=visible_group_ids)

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
        return await self.repo.list_page(
            search=search, filters=filters, sort=sort, direction=direction,
            attr_names=attr_names, limit=limit, offset=offset,
            visible_group_ids=visible_group_ids,
        )

    async def get(
        self, account_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> Account:
        a = await self.repo.get(account_id, visible_group_ids=visible_group_ids)
        if a is None:
            raise NotFoundError("Счёт не найден")
        return a

    async def get_many(
        self, ids: Sequence[uuid.UUID], *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> list[Account]:
        return await self.repo.get_many(ids, visible_group_ids=visible_group_ids)

    async def position_of(
        self, account_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> int | None:
        return await self.repo.position_of(account_id, visible_group_ids=visible_group_ids)

    async def list_for_client_ids(self, client_ids: Sequence[uuid.UUID]) -> list[Account]:
        return await self.repo.list_for_client_ids(client_ids)

    async def create(
        self, data: AccountCreate, *, allowed_group_ids: set[uuid.UUID] | None = None
    ) -> Account:
        # The parent client must be one the caller can see — enforces that you
        # can only attach an account under a visible client.
        client = await self.clients.get(data.client_id, visible_group_ids=allowed_group_ids)
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

    async def update(
        self,
        account_id: uuid.UUID,
        data: AccountUpdate,
        *,
        allowed_group_ids: set[uuid.UUID] | None = None,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> Account:
        account = await self.get(account_id, visible_group_ids=visible_group_ids)
        client = await self.clients.get(data.client_id, visible_group_ids=allowed_group_ids)
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

    async def delete(
        self, account_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> None:
        account = await self.get(account_id, visible_group_ids=visible_group_ids)
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
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> list[Card]:
        return await self.repo.list_all(
            account_id=account_id, client_id=client_id, visible_group_ids=visible_group_ids
        )

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
        return await self.repo.list_page(
            search=search, filters=filters, sort=sort, direction=direction,
            attr_names=attr_names, limit=limit, offset=offset,
            visible_group_ids=visible_group_ids,
        )

    async def get(
        self, card_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> Card:
        c = await self.repo.get(card_id, visible_group_ids=visible_group_ids)
        if c is None:
            raise NotFoundError("Карта не найдена")
        return c

    async def get_many(
        self, ids: Sequence[uuid.UUID], *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> list[Card]:
        return await self.repo.get_many(ids, visible_group_ids=visible_group_ids)

    async def position_of(
        self, card_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> int | None:
        return await self.repo.position_of(card_id, visible_group_ids=visible_group_ids)

    async def list_for_account_ids(self, account_ids: Sequence[uuid.UUID]) -> list[Card]:
        return await self.repo.list_for_account_ids(account_ids)

    async def list_for_client_ids(self, client_ids: Sequence[uuid.UUID]) -> list[Card]:
        return await self.repo.list_for_client_ids(client_ids)

    async def create(
        self, data: CardCreate, *, allowed_group_ids: set[uuid.UUID] | None = None
    ) -> Card:
        account = await self.accounts.get(data.account_id, visible_group_ids=allowed_group_ids)
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

    async def update(
        self,
        card_id: uuid.UUID,
        data: CardUpdate,
        *,
        allowed_group_ids: set[uuid.UUID] | None = None,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> Card:
        card = await self.get(card_id, visible_group_ids=visible_group_ids)
        account = await self.accounts.get(data.account_id, visible_group_ids=allowed_group_ids)
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

    async def delete(
        self, card_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> None:
        card = await self.get(card_id, visible_group_ids=visible_group_ids)
        await self.repo.delete(card)
