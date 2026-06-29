from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageSend

# Hard cap on history rows returned to the drawer table — same intent as the
# other list limits: bound the worst case without paging UI.
DEFAULT_HISTORY_LIMIT = 200


class LastSends:
    """Latest successful and latest failed send timestamps for one object.

    Either may be ``None`` (no send of that outcome yet). Feeds the «последняя
    успешная / неуспешная отправка» badges next to a send button.
    """

    __slots__ = ("success_at", "error_at")

    def __init__(self, success_at: datetime | None = None, error_at: datetime | None = None) -> None:
        self.success_at = success_at
        self.error_at = error_at


class MessageSendRepository:
    """Data access for the message send history (``message_sends``)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, item: MessageSend) -> MessageSend:
        self.session.add(item)
        await self.session.flush()
        return item

    async def list_for_filled(
        self, filled_id: uuid.UUID, *, limit: int = DEFAULT_HISTORY_LIMIT
    ) -> list[MessageSend]:
        """Sends of one filled template, newest first."""

        stmt = (
            select(MessageSend)
            .where(MessageSend.filled_template_id == filled_id)
            .order_by(MessageSend.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_chain(
        self, chain_id: uuid.UUID, *, limit: int = DEFAULT_HISTORY_LIMIT
    ) -> list[MessageSend]:
        """Sends of every step of one chain, newest first."""

        stmt = (
            select(MessageSend)
            .where(MessageSend.chain_id == chain_id)
            .order_by(MessageSend.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def last_for_filled(
        self, filled_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, LastSends]:
        """``{filled_id: LastSends}`` — latest success/error per filled template."""

        return await self._last_by(MessageSend.filled_template_id, filled_ids)

    async def last_for_chain_steps(
        self, step_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, LastSends]:
        """``{step_id: LastSends}`` — latest success/error per chain step."""

        return await self._last_by(MessageSend.chain_step_id, step_ids)

    async def last_for_chain(self, chain_id: uuid.UUID) -> LastSends:
        """Latest success/error across *all* sends of a chain (any step) —
        drives the «последний запуск» badge next to «Запустить всё»."""

        out = await self._last_by(MessageSend.chain_id, [chain_id])
        return out.get(chain_id, LastSends())

    async def _last_by(
        self, column: Any, ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, LastSends]:
        out: dict[uuid.UUID, LastSends] = {}
        if not ids:
            return out
        # Aggregate in SQL — two timestamps per id, regardless of history size —
        # rather than streaming every row back to reduce in Python. The partial
        # indexes-friendly FILTER splits success vs. failure in one GROUP BY pass.
        success_at = func.max(MessageSend.created_at).filter(MessageSend.ok.is_(True))
        error_at = func.max(MessageSend.created_at).filter(MessageSend.ok.is_(False))
        stmt = (
            select(column, success_at, error_at)
            .where(column.in_(list(ids)))
            .group_by(column)
        )
        for owner_id, last_ok_at, last_err_at in (await self.session.execute(stmt)).all():
            out[owner_id] = LastSends(success_at=last_ok_at, error_at=last_err_at)
        return out
