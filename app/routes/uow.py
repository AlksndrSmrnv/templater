from __future__ import annotations

from typing import TypeVar

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.errors import IntegrityViolation

T = TypeVar("T", bound=object)


async def commit_or_409(
    session: AsyncSession,
    *,
    message: str = "Операция нарушает ограничения целостности",
) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise IntegrityViolation(message) from exc


async def commit_and_refresh(
    session: AsyncSession,
    item: T,
    *,
    message: str = "Операция нарушает ограничения целостности",
) -> T:
    await commit_or_409(session, message=message)
    await session.refresh(item)
    return item
