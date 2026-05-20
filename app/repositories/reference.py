from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ReferenceValue


class ReferenceValueRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_by_type(self, entity_type: str) -> list[ReferenceValue]:
        stmt = (
            select(ReferenceValue)
            .where(ReferenceValue.entity_type == entity_type)
            .order_by(ReferenceValue.code)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, value_id: uuid.UUID) -> ReferenceValue | None:
        return await self.session.get(ReferenceValue, value_id)

    async def get_by_code(self, entity_type: str, code: str) -> ReferenceValue | None:
        stmt = select(ReferenceValue).where(
            ReferenceValue.entity_type == entity_type, ReferenceValue.code == code
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, value: ReferenceValue) -> ReferenceValue:
        self.session.add(value)
        await self.session.flush()
        return value

    async def delete(self, value: ReferenceValue) -> None:
        await self.session.delete(value)

    async def exists(self, entity_type: str, value_id: uuid.UUID) -> bool:
        stmt = select(ReferenceValue.id).where(
            ReferenceValue.id == value_id, ReferenceValue.entity_type == entity_type
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None
