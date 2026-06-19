from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class ImportCollectionSummary(BaseModel):
    """Outcome of importing one collection file."""

    collection_id: uuid.UUID
    name: str
    templates_created: int
    unparsable: int = 0


class ProcessCollectionSummary(BaseModel):
    """Outcome of running LLM analysis across a collection's templates."""

    processed: int = 0
    skipped: int = 0
    failed: int = 0


class CollectionJobProgress(BaseModel):
    """Snapshot of a background collection LLM job for the polling endpoint.

    ``status`` is ``pending|running|done|failed``. While in ``pending`` or
    ``running`` the frontend re-polls every second; ``done``/``failed`` are
    terminal. ``done`` carries the final per-template counts (some may be
    ``failed`` from individual LLM errors); ``failed`` means the orchestrator
    itself blew up and ``error`` holds the reason.
    """

    id: uuid.UUID
    collection_id: uuid.UUID
    status: str
    total: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def completed(self) -> int:
        """Templates with a known outcome (processed + skipped + failed)."""

        return self.processed + self.skipped + self.failed

    @property
    def is_terminal(self) -> bool:
        return self.status in ("done", "failed")

