from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.db.models import FilledTemplate

# Hard cap on rows returned by the list endpoint. The list page only
# displays name/format/snapshot labels/created_at — it never touches the
# heavy ``filled_content`` / ``changed_locations`` columns (those are
# deferred below) — but we still want a bound so a single search doesn't
# stream tens of thousands of rows. Bump if you actually need more.
DEFAULT_LIST_LIMIT = 200


class FilledTemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(
        self,
        *,
        search: str = "",
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[FilledTemplate]:
        """Return up to ``limit`` rows with heavy text columns deferred.

        ``filled_content`` (full rendered body) and ``changed_locations``
        (per-leaf JSONPath/XPath list) are not used by the list page, so
        they are deferred: accessing them on a returned row would trigger
        an extra SELECT. Templates rendering this list MUST NOT read those
        attributes — use ``get()`` for the detail page instead.
        """

        stmt = (
            select(FilledTemplate)
            .options(
                defer(FilledTemplate.filled_content),
                defer(FilledTemplate.changed_locations),
            )
            .order_by(FilledTemplate.created_at.desc())
            .limit(limit)
        )
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

    async def list_by_template(
        self, template_id: uuid.UUID, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[FilledTemplate]:
        """Filled snapshots produced from a given template (newest first).

        Heavy text columns are deferred — the caller only renders name/date
        links. Used by the template workspace panel to show «связанные
        заполненные шаблоны».
        """

        stmt = (
            select(FilledTemplate)
            .options(
                defer(FilledTemplate.filled_content),
                defer(FilledTemplate.changed_locations),
            )
            .where(FilledTemplate.message_template_id == template_id)
            .order_by(FilledTemplate.created_at.desc())
            .limit(limit)
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
