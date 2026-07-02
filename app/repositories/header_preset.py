from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import HeaderPreset


class HeaderPresetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[HeaderPreset]:
        stmt = select(HeaderPreset).order_by(HeaderPreset.name)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_by_project(self, project_id: uuid.UUID) -> list[HeaderPreset]:
        stmt = (
            select(HeaderPreset)
            .where(HeaderPreset.project_id == project_id)
            .order_by(HeaderPreset.name)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, preset_id: uuid.UUID) -> HeaderPreset | None:
        return await self.session.get(HeaderPreset, preset_id)

    async def get_many(self, preset_ids: list[uuid.UUID]) -> list[HeaderPreset]:
        if not preset_ids:
            return []
        stmt = select(HeaderPreset).where(HeaderPreset.id.in_(preset_ids))
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_by_name_in_project(
        self, project_id: uuid.UUID, name: str
    ) -> HeaderPreset | None:
        stmt = select(HeaderPreset).where(
            HeaderPreset.project_id == project_id, HeaderPreset.name == name
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, preset: HeaderPreset) -> HeaderPreset:
        self.session.add(preset)
        await self.session.flush()
        return preset

    async def delete(self, preset: HeaderPreset) -> None:
        await self.session.delete(preset)
