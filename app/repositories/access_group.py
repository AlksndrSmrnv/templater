from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AccessGroup, Client, FilledTemplate


class AccessGroupRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[AccessGroup]:
        stmt = select(AccessGroup).order_by(AccessGroup.name)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, group_id: uuid.UUID) -> AccessGroup | None:
        return await self.session.get(AccessGroup, group_id)

    async def get_by_name(self, name: str) -> AccessGroup | None:
        stmt = select(AccessGroup).where(AccessGroup.name == name)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, group: AccessGroup) -> AccessGroup:
        self.session.add(group)
        await self.session.flush()
        return group

    async def delete(self, group: AccessGroup) -> None:
        await self.session.delete(group)

    async def count_clients(self, group_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(Client).where(Client.group_id == group_id)
        return int((await self.session.execute(stmt)).scalar_one())

    async def count_filled(self, group_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(FilledTemplate)
            .where(FilledTemplate.group_id == group_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())
