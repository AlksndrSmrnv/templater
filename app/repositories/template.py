from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import MessageTemplate


def _collection_filter(collection_id: uuid.UUID | None) -> ColumnElement[bool]:
    """``collection_id`` match where ``None`` means the ungrouped/root space."""

    if collection_id is None:
        return MessageTemplate.collection_id.is_(None)
    return MessageTemplate.collection_id == collection_id


class TemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[MessageTemplate]:
        stmt = select(MessageTemplate).order_by(MessageTemplate.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_by_collection(self, collection_id: uuid.UUID) -> list[MessageTemplate]:
        stmt = (
            select(MessageTemplate)
            .where(MessageTemplate.collection_id == collection_id)
            .order_by(MessageTemplate.display_order, MessageTemplate.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_ungrouped(self) -> list[MessageTemplate]:
        stmt = (
            select(MessageTemplate)
            .where(MessageTemplate.collection_id.is_(None))
            .order_by(MessageTemplate.display_order, MessageTemplate.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_by_folder(
        self, collection_id: uuid.UUID | None, folder_path: list[str]
    ) -> list[MessageTemplate]:
        """Rows living exactly in ``folder_path`` of the given collection
        (``None`` = ungrouped/root) — the sibling set for drag-and-drop
        renumbering."""

        stmt = (
            select(MessageTemplate)
            .where(
                _collection_filter(collection_id),
                MessageTemplate.folder_path == folder_path,
            )
            .order_by(MessageTemplate.display_order, MessageTemplate.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def next_display_order(
        self, collection_id: uuid.UUID | None, folder_path: list[str]
    ) -> int:
        """1 + the highest ``display_order`` in the folder (0 when empty) —
        new rows append after the manually ordered siblings."""

        stmt = select(
            func.coalesce(func.max(MessageTemplate.display_order), -1)
        ).where(
            _collection_filter(collection_id),
            MessageTemplate.folder_path == folder_path,
        )
        return int((await self.session.execute(stmt)).scalar_one()) + 1

    async def delete_by_collection(self, collection_id: uuid.UUID) -> int:
        templates = await self.list_by_collection(collection_id)
        for template in templates:
            await self.session.delete(template)
        return len(templates)

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
