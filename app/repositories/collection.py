from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Collection


class CollectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[Collection]:
        stmt = select(Collection).order_by(Collection.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, collection_id: uuid.UUID) -> Collection | None:
        return await self.session.get(Collection, collection_id)

    async def add(self, collection: Collection) -> Collection:
        self.session.add(collection)
        await self.session.flush()
        return collection

    async def delete(self, collection: Collection) -> None:
        await self.session.delete(collection)
