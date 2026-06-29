"""MessageSendRepository — the per-object «latest success / latest error»
lookup, on a fake session (no live DB).

``_last_by`` aggregates in SQL (``max(created_at) FILTER (WHERE ok)`` /
``... FILTER (WHERE NOT ok)`` GROUP BY fk), so the query yields one row per id of
``(id, last_success_at, last_error_at)``; the fake returns exactly that shape.
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
async def test_last_for_steps_maps_success_and_error() -> None:
    step = uuid.uuid4()
    # One aggregated row per id: (id, last_success_at, last_error_at).
    rows = [(step, datetime(2026, 6, 29, 12, 0, 2), datetime(2026, 6, 29, 12, 0, 1))]
    repo = MessageSendRepository(_FakeSession(rows))  # type: ignore[arg-type]
    out = await repo.last_for_chain_steps([step])
    assert out[step].success_at == datetime(2026, 6, 29, 12, 0, 2)
    assert out[step].error_at == datetime(2026, 6, 29, 12, 0, 1)


@pytest.mark.asyncio
async def test_last_for_filled_missing_outcome_is_none() -> None:
    fid = uuid.uuid4()
    rows = [(fid, datetime(2026, 6, 29, 9, 0, 0), None)]  # only a success ever
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


@pytest.mark.asyncio
async def test_last_for_chain_returns_aggregate_for_the_chain() -> None:
    chain = uuid.uuid4()
    rows = [(chain, datetime(2026, 6, 29, 14, 0, 0), datetime(2026, 6, 29, 13, 0, 0))]
    repo = MessageSendRepository(_FakeSession(rows))  # type: ignore[arg-type]
    last = await repo.last_for_chain(chain)
    assert last.success_at == datetime(2026, 6, 29, 14, 0, 0)
    assert last.error_at == datetime(2026, 6, 29, 13, 0, 0)


@pytest.mark.asyncio
async def test_last_for_chain_no_history_is_empty() -> None:
    # No rows → both timestamps None (chain never run).
    repo = MessageSendRepository(_FakeSession([]))  # type: ignore[arg-type]
    last = await repo.last_for_chain(uuid.uuid4())
    assert last.success_at is None
    assert last.error_at is None
