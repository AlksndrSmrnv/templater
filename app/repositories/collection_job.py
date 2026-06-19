from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CollectionJob

# Statuses that count as "still active" — only one such job may exist per
# collection at a time (enforced by CollectionJobService.start).
_ACTIVE_STATUSES = ("pending", "running")


class CollectionJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, job: CollectionJob) -> CollectionJob:
        self.session.add(job)
        await self.session.flush()
        return job

    async def get(self, job_id: uuid.UUID) -> CollectionJob | None:
        return await self.session.get(CollectionJob, job_id)

    async def find_active(self, collection_id: uuid.UUID) -> CollectionJob | None:
        """A pending/running job for the collection, if any — used to refuse a
        duplicate start before creating a new row."""

        stmt = (
            select(CollectionJob)
            .where(
                CollectionJob.collection_id == collection_id,
                CollectionJob.status.in_(_ACTIVE_STATUSES),
            )
            .order_by(CollectionJob.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def mark_running(self, job_id: uuid.UUID, started_at: datetime) -> None:
        await self.session.execute(
            update(CollectionJob)
            .where(CollectionJob.id == job_id)
            .values(status="running", started_at=started_at)
        )

    async def set_total(self, job_id: uuid.UUID, total: int) -> None:
        await self.session.execute(
            update(CollectionJob)
            .where(CollectionJob.id == job_id)
            .values(total=total)
        )

    async def increment(
        self,
        job_id: uuid.UUID,
        *,
        processed: int = 0,
        skipped: int = 0,
        failed: int = 0,
    ) -> None:
        """Bump the per-template counters atomically (no read-modify-write)."""

        values: dict[str, object] = {}
        if processed:
            values["processed"] = CollectionJob.processed + processed
        if skipped:
            values["skipped"] = CollectionJob.skipped + skipped
        if failed:
            values["failed"] = CollectionJob.failed + failed
        if not values:
            return
        await self.session.execute(
            update(CollectionJob).where(CollectionJob.id == job_id).values(values)
        )

    async def mark_done(self, job_id: uuid.UUID, finished_at: datetime) -> None:
        await self.session.execute(
            update(CollectionJob)
            .where(CollectionJob.id == job_id)
            .values(status="done", finished_at=finished_at)
        )

    async def mark_failed(
        self, job_id: uuid.UUID, error: str, finished_at: datetime
    ) -> None:
        await self.session.execute(
            update(CollectionJob)
            .where(CollectionJob.id == job_id)
            .values(status="failed", error=error, finished_at=finished_at)
        )

    async def reconcile(self, *, error: str, finished_at: datetime) -> int:
        """On startup, mark any pending/running rows (whose in-process task is
        gone with the previous process) as ``failed``. Returns the row count."""

        result = await self.session.execute(
            update(CollectionJob)
            .where(CollectionJob.status.in_(_ACTIVE_STATUSES))
            .values(status="failed", error=error, finished_at=finished_at)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
