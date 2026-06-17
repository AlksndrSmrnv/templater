from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.db.models import FilledTemplate
from app.repositories.entity import group_visibility_condition

# Hard cap on rows returned by the list endpoint. The list page only
# displays name/format/snapshot labels/created_at — it never touches the
# heavy ``filled_content`` / ``changed_locations`` columns (those are
# deferred below) — but we still want a bound so a single search doesn't
# stream tens of thousands of rows. Bump if you actually need more.
DEFAULT_LIST_LIMIT = 200

# Heavy columns the tree/list views never read; deferred so accessing them
# on a listed row would trigger an extra SELECT (use ``get()`` for details).
_LIST_DEFERS = (
    defer(FilledTemplate.filled_content),
    defer(FilledTemplate.changed_locations),
    defer(FilledTemplate.headers_snapshot),
)


class FilledTemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(
        self,
        *,
        search: str = "",
        limit: int | None = DEFAULT_LIST_LIMIT,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> list[FilledTemplate]:
        """Return up to ``limit`` rows with heavy text columns deferred.

        ``filled_content`` (full rendered body), ``changed_locations``
        (per-leaf JSONPath/XPath list) and ``headers_snapshot`` are not used
        by the tree/list views, so they are deferred. Templates rendering
        this list MUST NOT read those attributes — use ``get()`` for the
        detail page instead. ``limit=None`` disables the cap.

        ``visible_group_ids`` filters to public rows plus rows in an unlocked
        group; ``None`` returns everything (internal callers).
        """

        stmt = (
            select(FilledTemplate)
            .options(*_LIST_DEFERS)
            .order_by(FilledTemplate.created_at.desc())
        )
        cond = group_visibility_condition(FilledTemplate, visible_group_ids)
        if cond is not None:
            stmt = stmt.where(cond)
        if limit is not None:
            stmt = stmt.limit(limit)
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
        self,
        template_id: uuid.UUID,
        *,
        limit: int = DEFAULT_LIST_LIMIT,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> list[FilledTemplate]:
        """Filled snapshots produced from a given template (newest first).

        Heavy text columns are deferred — the caller only renders name/date
        links. Used by the template workspace panel to show «связанные
        заполненные шаблоны». Filtered to the caller's visible groups so the
        public template panel never leaks a private fill's label.
        """

        stmt = (
            select(FilledTemplate)
            .options(*_LIST_DEFERS)
            .where(FilledTemplate.message_template_id == template_id)
            .order_by(FilledTemplate.created_at.desc())
            .limit(limit)
        )
        cond = group_visibility_condition(FilledTemplate, visible_group_ids)
        if cond is not None:
            stmt = stmt.where(cond)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_folder_paths(self) -> list[list[str]]:
        """Just the ``folder_path`` column of every row.

        Folder existence/emptiness checks and the save-form selector need
        paths only — selecting the single JSONB column keeps these operations
        cheap no matter how many rows the table holds.
        """

        rows = (
            await self.session.execute(select(FilledTemplate.folder_path))
        ).scalars().all()
        return [list(path or []) for path in rows]

    async def list_ids_with_paths(self) -> list[tuple[uuid.UUID, list[str]]]:
        """(id, folder_path) projection of every row — lets folder rename find
        descendants without materialising full ORM rows for the whole table."""

        rows = (
            await self.session.execute(
                select(FilledTemplate.id, FilledTemplate.folder_path)
            )
        ).all()
        return [(row_id, list(path or [])) for row_id, path in rows]

    async def list_by_folder(self, folder_path: list[str]) -> list[FilledTemplate]:
        """Rows living exactly in ``folder_path`` (heavy columns deferred) —
        the sibling set for drag-and-drop renumbering."""

        stmt = (
            select(FilledTemplate)
            .options(*_LIST_DEFERS)
            .where(FilledTemplate.folder_path == folder_path)
            .order_by(FilledTemplate.display_order, FilledTemplate.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def next_display_order(self, folder_path: list[str]) -> int:
        """1 + the highest ``display_order`` in ``folder_path`` (0 when empty)
        — new rows append after the manually ordered siblings."""

        stmt = select(
            func.coalesce(func.max(FilledTemplate.display_order), -1)
        ).where(FilledTemplate.folder_path == folder_path)
        return int((await self.session.execute(stmt)).scalar_one()) + 1

    async def get(
        self, filled_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> FilledTemplate | None:
        if visible_group_ids is None:
            return await self.session.get(FilledTemplate, filled_id)
        stmt = select(FilledTemplate).where(
            FilledTemplate.id == filled_id,
            group_visibility_condition(FilledTemplate, visible_group_ids),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_many(self, ids: Sequence[uuid.UUID]) -> list[FilledTemplate]:
        """Rows by id (heavy columns deferred) — folder rename loads only the
        descendants it needs to re-prefix."""

        if not ids:
            return []
        stmt = (
            select(FilledTemplate)
            .options(*_LIST_DEFERS)
            .where(FilledTemplate.id.in_(ids))
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def add(self, item: FilledTemplate) -> FilledTemplate:
        self.session.add(item)
        await self.session.flush()
        return item

    async def delete(self, item: FilledTemplate) -> None:
        await self.session.delete(item)
