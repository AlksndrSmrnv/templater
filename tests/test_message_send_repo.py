"""MessageSendRepository — the per-object «latest success / latest error»
reduction, on a fake session (no live DB).

``_last_by`` relies on the query returning rows newest-first; the fake mirrors
that ordering so the reduction picks the first success and first error seen per
id as the latest of each.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest

from app.repositories.message_send import MessageSendRepository


class _FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.executed = False

    async def execute(self, _stmt: Any) -> _FakeResult:
        self.executed = True
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_last_for_steps_picks_latest_success_and_error() -> None:
    step = uuid.uuid4()
    # Newest-first (as the SQL orders): latest is a success, an earlier error.
    rows = [
        (step, True, datetime(2026, 6, 29, 12, 0, 2)),
        (step, False, datetime(2026, 6, 29, 12, 0, 1)),
        (step, True, datetime(2026, 6, 29, 12, 0, 0)),
    ]
    repo = MessageSendRepository(_FakeSession(rows))  # type: ignore[arg-type]
    out = await repo.last_for_chain_steps([step])
    assert out[step].success_at == datetime(2026, 6, 29, 12, 0, 2)
    assert out[step].error_at == datetime(2026, 6, 29, 12, 0, 1)


@pytest.mark.asyncio
async def test_last_for_filled_missing_outcome_is_none() -> None:
    fid = uuid.uuid4()
    rows = [(fid, True, datetime(2026, 6, 29, 9, 0, 0))]  # only a success ever
    repo = MessageSendRepository(_FakeSession(rows))  # type: ignore[arg-type]
    out = await repo.last_for_filled([fid])
    assert out[fid].success_at == datetime(2026, 6, 29, 9, 0, 0)
    assert out[fid].error_at is None


@pytest.mark.asyncio
async def test_last_for_empty_ids_skips_query() -> None:
    session = _FakeSession([])
    repo = MessageSendRepository(session)  # type: ignore[arg-type]
    out = await repo.last_for_chain_steps([])
    assert out == {}
    assert session.executed is False
