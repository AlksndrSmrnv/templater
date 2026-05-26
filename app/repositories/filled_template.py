from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FilledTemplate


class FilledTemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self, *, search: str = "") -> list[FilledTemplate]:
        stmt = select(FilledTemplate).order_by(FilledTemplate.created_at.desc())
        term = search.strip()
        if term:
            like = f"%{term}%"
            stmt = stmt.where(
                or_(
                    FilledTemplate.name.ilike(like),
                    FilledTemplate.template_name_snapshot.ilike(like),
                )
            )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, filled_id: uuid.UUID) -> FilledTemplate | None:
        return await self.session.get(FilledTemplate, filled_id)

    async def add(self, item: FilledTemplate) -> FilledTemplate:
        self.session.add(item)
        await self.session.flush()
        return item

    async def delete(self, item: FilledTemplate) -> None:
        await self.session.delete(item)
