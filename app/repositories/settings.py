from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSetting


class SettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, key: str, default: Any = None) -> Any:
        result = await self.session.execute(select(AppSetting.value).where(AppSetting.key == key))
        value = result.scalar_one_or_none()
        return value if value is not None else default

    async def set(self, key: str, value: Any) -> None:
        stmt = (
            pg_insert(AppSetting)
            .values(key=key, value=value)
            .on_conflict_do_update(index_elements=["key"], set_={"value": value})
        )
        await self.session.execute(stmt)

    async def all(self) -> dict[str, Any]:
        rows = (await self.session.execute(select(AppSetting.key, AppSetting.value))).all()
        return {key: value for key, value in rows}
