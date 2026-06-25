from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import RequestChain, RequestChainStep
from app.repositories.entity import group_visibility_condition

# Hard cap on chains returned by the tree/list — same intent as the filled
# template list limit: bound the worst case without paging UI.
DEFAULT_LIST_LIMIT = 200


class RequestChainRepository:
    """Data access for request chains and their ordered steps.

    Chains share the «Заполненные шаблоны» folder tree, so this repo exposes the
    same lightweight ``folder_path`` projections the folder operations need
    (existence/rename/reorder) as :class:`FilledTemplateRepository`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(
        self,
        *,
        limit: int | None = DEFAULT_LIST_LIMIT,
        visible_group_ids: set[uuid.UUID] | None = None,
    ) -> list[RequestChain]:
        """Chains visible to the caller (newest first). Steps are NOT loaded —
        the tree only needs name/folder/method counts, so keep it cheap."""

        stmt = select(RequestChain).order_by(RequestChain.created_at.desc())
        cond = group_visibility_condition(RequestChain, visible_group_ids)
        if cond is not None:
            stmt = stmt.where(cond)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(
        self, chain_id: uuid.UUID, *, visible_group_ids: set[uuid.UUID] | None = None
    ) -> RequestChain | None:
        """One chain with its steps eagerly loaded (ordered by position)."""

        stmt = (
            select(RequestChain)
            .options(selectinload(RequestChain.steps))
            .where(RequestChain.id == chain_id)
        )
        cond = group_visibility_condition(RequestChain, visible_group_ids)
        if cond is not None:
            stmt = stmt.where(cond)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_folder_paths(self) -> list[list[str]]:
        """``folder_path`` of every chain — feeds folder existence/emptiness
        checks shared with filled templates."""

        rows = (
            await self.session.execute(select(RequestChain.folder_path))
        ).scalars().all()
        return [list(path or []) for path in rows]

    async def list_ids_with_paths(self) -> list[tuple[uuid.UUID, list[str]]]:
        """(id, folder_path) projection — lets folder rename re-prefix chains
        without materialising full rows."""

        rows = (
            await self.session.execute(
                select(RequestChain.id, RequestChain.folder_path)
            )
        ).all()
        return [(row_id, list(path or [])) for row_id, path in rows]

    async def list_by_folder(self, folder_path: list[str]) -> list[RequestChain]:
        """Chains living exactly in ``folder_path`` — the sibling set for
        drag-and-drop renumbering (steps not loaded)."""

        stmt = (
            select(RequestChain)
            .where(RequestChain.folder_path == folder_path)
            .order_by(RequestChain.display_order, RequestChain.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def next_display_order(self, folder_path: list[str]) -> int:
        """1 + the highest ``display_order`` in ``folder_path`` (0 when empty)."""

        stmt = select(
            func.coalesce(func.max(RequestChain.display_order), -1)
        ).where(RequestChain.folder_path == folder_path)
        return int((await self.session.execute(stmt)).scalar_one()) + 1

    async def step_counts(self) -> dict[uuid.UUID, int]:
        """``{chain_id: step_count}`` for every chain with at least one step —
        lets the tree show a «N шагов» badge without lazy-loading ``.steps``."""

        stmt = select(
            RequestChainStep.chain_id, func.count()
        ).group_by(RequestChainStep.chain_id)
        rows = (await self.session.execute(stmt)).all()
        return {chain_id: int(count) for chain_id, count in rows}

    async def get_many(self, ids: Sequence[uuid.UUID]) -> list[RequestChain]:
        if not ids:
            return []
        stmt = select(RequestChain).where(RequestChain.id.in_(ids))
        return list((await self.session.execute(stmt)).scalars().all())

    async def add(self, chain: RequestChain) -> RequestChain:
        self.session.add(chain)
        await self.session.flush()
        return chain

    async def delete(self, chain: RequestChain) -> None:
        await self.session.delete(chain)

    # ---- steps ----

    async def get_step(self, step_id: uuid.UUID) -> RequestChainStep | None:
        return await self.session.get(RequestChainStep, step_id)

    async def next_position(self, chain_id: uuid.UUID) -> int:
        """1 + the highest step ``position`` in the chain (0 when empty)."""

        stmt = select(
            func.coalesce(func.max(RequestChainStep.position), -1)
        ).where(RequestChainStep.chain_id == chain_id)
        return int((await self.session.execute(stmt)).scalar_one()) + 1

    async def list_steps(self, chain_id: uuid.UUID) -> list[RequestChainStep]:
        stmt = (
            select(RequestChainStep)
            .where(RequestChainStep.chain_id == chain_id)
            .order_by(RequestChainStep.position, RequestChainStep.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def add_step(self, step: RequestChainStep) -> RequestChainStep:
        self.session.add(step)
        await self.session.flush()
        return step

    async def delete_step(self, step: RequestChainStep) -> None:
        await self.session.delete(step)
