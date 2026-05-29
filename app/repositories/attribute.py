from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AttributeDefinition


class AttributeDefinitionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_by_entity(self, entity_type: str) -> list[AttributeDefinition]:
        stmt = (
            select(AttributeDefinition)
            .where(AttributeDefinition.entity_type == entity_type)
            .order_by(AttributeDefinition.display_order, AttributeDefinition.name)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_all(self) -> list[AttributeDefinition]:
        stmt = select(AttributeDefinition).order_by(
            AttributeDefinition.entity_type,
            AttributeDefinition.display_order,
            AttributeDefinition.name,
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_by_id(self, attr_id: uuid.UUID) -> AttributeDefinition | None:
        return await self.session.get(AttributeDefinition, attr_id)

    async def get_by_name(self, entity_type: str, name: str) -> AttributeDefinition | None:
        stmt = select(AttributeDefinition).where(
            AttributeDefinition.entity_type == entity_type,
            AttributeDefinition.name == name,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, attr: AttributeDefinition) -> AttributeDefinition:
        self.session.add(attr)
        await self.session.flush()
        return attr

    async def delete(self, attr: AttributeDefinition) -> None:
        await self.session.delete(attr)
