from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
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

    async def _last_by(
        self, column: Any, ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, LastSends]:
        out: dict[uuid.UUID, LastSends] = {}
        if not ids:
            return out
        # Newest first, so the first success/error seen per id is the latest.
        stmt = (
            select(column, MessageSend.ok, MessageSend.created_at)
            .where(column.in_(list(ids)))
            .order_by(MessageSend.created_at.desc())
        )
        for owner_id, ok, created_at in (await self.session.execute(stmt)).all():
            last = out.setdefault(owner_id, LastSends())
            if ok and last.success_at is None:
                last.success_at = created_at
            elif not ok and last.error_at is None:
                last.error_at = created_at
        return out
