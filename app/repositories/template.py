from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageTemplate


class TemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[MessageTemplate]:
        stmt = select(MessageTemplate).order_by(MessageTemplate.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, template_id: uuid.UUID) -> MessageTemplate | None:
        return await self.session.get(MessageTemplate, template_id)

    async def get_many(self, ids: Sequence[uuid.UUID]) -> list[MessageTemplate]:
        if not ids:
            return []
        stmt = select(MessageTemplate).where(MessageTemplate.id.in_(ids))
        return list((await self.session.execute(stmt)).scalars().all())

    async def add(self, template: MessageTemplate) -> MessageTemplate:
        self.session.add(template)
        await self.session.flush()
        return template

    async def delete(self, template: MessageTemplate) -> None:
        await self.session.delete(template)
