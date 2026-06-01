from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ReferenceType


class ReferenceTypeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[ReferenceType]:
        stmt = select(ReferenceType).order_by(
            ReferenceType.display_order, ReferenceType.code
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, code: str) -> ReferenceType | None:
        return await self.session.get(ReferenceType, code)

    async def codes(self) -> list[str]:
        stmt = select(ReferenceType.code).order_by(
            ReferenceType.display_order, ReferenceType.code
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def exists(self, code: str) -> bool:
        stmt = select(ReferenceType.code).where(ReferenceType.code == code)
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def add(self, ref_type: ReferenceType) -> ReferenceType:
        self.session.add(ref_type)
        await self.session.flush()
        return ref_type

    async def delete(self, ref_type: ReferenceType) -> None:
        await self.session.delete(ref_type)
