"""RequestChainService — chain/step CRUD on fakes (no live DB).

Follows the project's test conventions: SimpleNamespace rows, in-memory fake
repositories, and an explicit ``flush()`` that models the real session's
``autoflush=False`` (a query sees only flushed state).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.services.request_chain import RequestChainService
from app.utils.errors import NotFoundError, ValidationFailed


class _FakeSession:
    async def flush(self) -> None:  # autoflush is off; service flushes explicitly
        return None


class _FakeChainRepo:
    def __init__(self) -> None:
        self.chains: list[SimpleNamespace] = []
        self.steps: list[SimpleNamespace] = []
        self._clock = datetime(2026, 6, 25, 12, 0)

    def _tick(self) -> datetime:
        self._clock += timedelta(seconds=1)
        return self._clock

    # chains
    async def get(self, chain_id, *, visible_group_ids=None):
        chain = next((c for c in self.chains if c.id == chain_id), None)
        if chain is not None:
            chain.steps = sorted(
                (s for s in self.steps if s.chain_id == chain_id),
                key=lambda s: (s.position, s.created_at),
            )
        return chain

    async def list_all(self, *, limit=200, visible_group_ids=None):
        return list(self.chains)

    async def list_folder_paths(self):
        return [list(c.folder_path or []) for c in self.chains]

    async def next_display_order(self, folder_path):
        orders = [c.display_order for c in self.chains if list(c.folder_path or []) == list(folder_path)]
        return (max(orders) + 1) if orders else 0

    async def add(self, chain):
        if getattr(chain, "id", None) is None:
            chain.id = uuid.uuid4()
        self.chains.append(chain)
        return chain

    async def delete(self, chain):
        self.chains = [c for c in self.chains if c.id != chain.id]
        self.steps = [s for s in self.steps if s.chain_id != chain.id]

    # steps
    async def get_step(self, step_id):
        return next((s for s in self.steps if s.id == step_id), None)

    async def next_position(self, chain_id):
        ps = [s.position for s in self.steps if s.chain_id == chain_id]
        return (max(ps) + 1) if ps else 0

    async def list_steps(self, chain_id):
        return sorted(
            (s for s in self.steps if s.chain_id == chain_id),
            key=lambda s: (s.position, s.created_at),
        )

    async def add_step(self, step):
        if getattr(step, "id", None) is None:
            step.id = uuid.uuid4()
        if getattr(step, "created_at", None) is None:
            step.created_at = self._tick()
        self.steps.append(step)
        return step

    async def delete_step(self, step):
        self.steps = [s for s in self.steps if s.id != step.id]


class _FakeFilledRepo:
    def __init__(self, filled: list[SimpleNamespace]) -> None:
        self.filled = filled

    async def get(self, filled_id, *, visible_group_ids=None):
        return next((f for f in self.filled if f.id == filled_id), None)

    async def list_folder_paths(self):
        return [list(f.folder_path or []) for f in self.filled]


class _FakeSettingsRepo:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = dict(values or {})

    async def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)


def _filled(
    *, name: str = "FT", group_id: uuid.UUID | None = None, group_name: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        format="json",
        http_method_snapshot="POST",
        url_snapshot="https://api.example/x",
        headers_snapshot=[{"key": "X", "value": "1"}],
        filled_content='{"amount": 100}',
        changed_locations=["/amount"],
        folder_path=[],
        group_id=group_id,
        group_name_snapshot=group_name,
        group_color_snapshot="#fff",
    )


def _service(
    filled: list[SimpleNamespace] | None = None,
    *,
    settings: _FakeSettingsRepo | None = None,
) -> RequestChainService:
    svc = RequestChainService(cast(Any, _FakeSession()))
    svc.repo = _FakeChainRepo()  # type: ignore[assignment]
    svc.filled = _FakeFilledRepo(filled or [])  # type: ignore[assignment]
    svc.settings = settings or _FakeSettingsRepo()  # type: ignore[assignment]
    return svc


# ---- create / rename / delete -------------------------------------------------


@pytest.mark.asyncio
async def test_create_chain_at_root_and_rejects_blank() -> None:
    svc = _service()
    chain = await svc.create_chain([], "Перевод")
    assert chain.name == "Перевод"
    assert chain.folder_path == []
    with pytest.raises(ValidationFailed):
        await svc.create_chain([], "   ")


@pytest.mark.asyncio
async def test_create_chain_in_known_folder_and_rejects_ghost() -> None:
    settings = _FakeSettingsRepo({"filled_root_folders": [["Проект"]]})
    svc = _service(settings=settings)
    chain = await svc.create_chain(["Проект"], "Релизная цепочка")
    assert chain.folder_path == ["Проект"]
    with pytest.raises(ValidationFailed):
        await svc.create_chain(["Призрак"], "x")


@pytest.mark.asyncio
async def test_rename_and_delete_chain() -> None:
    svc = _service()
    chain = await svc.create_chain([], "Старое")
    await svc.rename_chain(chain.id, "Новое")
    assert chain.name == "Новое"
    await svc.delete_chain(chain.id)
    with pytest.raises(NotFoundError):
        await svc.get(chain.id)


# ---- steps --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_step_snapshots_filled_envelope() -> None:
    ft = _filled(name="Создать перевод")
    svc = _service([ft])
    chain = await svc.create_chain([], "Цепочка")

    step = await svc.add_step(chain.id, ft.id)
    assert step.position == 0
    assert step.name_snapshot == "Создать перевод"
    assert step.url_snapshot == "https://api.example/x"
    assert step.body == '{"amount": 100}'
    assert step.filled_template_id == ft.id
    # Green-field locations snapshotted from the filled template.
    assert step.changed_locations == ["/amount"]
    assert step.bindings == {}
    # mock_response seeded with a realistic example.
    assert "transferId" in step.mock_response

    # Second step appends after the first.
    ft2 = _filled(name="Подтвердить")
    svc.filled.filled.append(ft2)  # type: ignore[attr-defined]
    step2 = await svc.add_step(chain.id, ft2.id)
    assert step2.position == 1


@pytest.mark.asyncio
async def test_add_step_missing_filled_raises() -> None:
    svc = _service([])
    chain = await svc.create_chain([], "Цепочка")
    with pytest.raises(NotFoundError):
        await svc.add_step(chain.id, uuid.uuid4())


@pytest.mark.asyncio
async def test_add_step_inherits_group_then_rejects_conflict() -> None:
    g1, g2 = uuid.uuid4(), uuid.uuid4()
    public = _filled(name="public")
    private1 = _filled(name="p1", group_id=g1, group_name="Группа A")
    private2 = _filled(name="p2", group_id=g2, group_name="Группа B")
    svc = _service([public, private1, private2])
    chain = await svc.create_chain([], "Цепочка")

    await svc.add_step(chain.id, public.id)
    assert chain.group_id is None  # public leaves it open

    await svc.add_step(chain.id, private1.id)
    assert chain.group_id == g1
    assert chain.group_name_snapshot == "Группа A"

    with pytest.raises(ValidationFailed):
        await svc.add_step(chain.id, private2.id)  # different group


@pytest.mark.asyncio
async def test_remove_step_renumbers_remaining() -> None:
    fts = [_filled(name=f"s{i}") for i in range(3)]
    svc = _service(fts)
    chain = await svc.create_chain([], "Цепочка")
    steps = [await svc.add_step(chain.id, f.id) for f in fts]

    await svc.remove_step(chain.id, steps[0].id)
    remaining = await svc.repo.list_steps(chain.id)  # type: ignore[attr-defined]
    assert [s.position for s in remaining] == [0, 1]
    assert [s.name_snapshot for s in remaining] == ["s1", "s2"]


@pytest.mark.asyncio
async def test_reorder_steps_follows_payload_and_renumbers() -> None:
    fts = [_filled(name=f"s{i}") for i in range(3)]
    svc = _service(fts)
    chain = await svc.create_chain([], "Цепочка")
    steps = [await svc.add_step(chain.id, f.id) for f in fts]

    # Move the last step to the front; unknown ids ignored.
    await svc.reorder_steps(chain.id, [steps[2].id, uuid.uuid4()])
    ordered = await svc.repo.list_steps(chain.id)  # type: ignore[attr-defined]
    assert [s.name_snapshot for s in ordered] == ["s2", "s0", "s1"]
    assert [s.position for s in ordered] == [0, 1, 2]


@pytest.mark.asyncio
async def test_update_step_body_and_mock() -> None:
    ft = _filled()
    svc = _service([ft])
    chain = await svc.create_chain([], "Цепочка")
    step = await svc.add_step(chain.id, ft.id)

    await svc.update_step(chain.id, step.id, body='{"x": "{{ $1.id }}"}', mock_response='{"id": 7}')
    assert step.body == '{"x": "{{ $1.id }}"}'
    assert step.mock_response == '{"id": 7}'

    with pytest.raises(NotFoundError):
        await svc.update_step(chain.id, uuid.uuid4(), body="x")


# ---- bind / unbind ------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_field_replaces_leaf_and_buffers_original() -> None:
    ft = _filled()  # body '{"amount": 100}'
    svc = _service([ft])
    chain = await svc.create_chain([], "Цепочка")
    step = await svc.add_step(chain.id, ft.id)

    await svc.bind_field(
        chain.id, step.id, location="/amount", ref_step=1, ref_path="transferId"
    )
    # The reference lives inline in the body…
    assert "{{ $1.transferId }}" in step.body
    # …and the original literal is buffered for «Сбросить».
    assert step.bindings == {"/amount": "100"}


@pytest.mark.asyncio
async def test_bind_field_rebind_keeps_first_original() -> None:
    ft = _filled()
    svc = _service([ft])
    chain = await svc.create_chain([], "Цепочка")
    step = await svc.add_step(chain.id, ft.id)

    await svc.bind_field(chain.id, step.id, location="/amount", ref_step=1, ref_path="a")
    await svc.bind_field(chain.id, step.id, location="/amount", ref_step=1, ref_path="b")
    # Re-binding must not overwrite the buffered literal with the prior token.
    assert step.bindings == {"/amount": "100"}
    assert "{{ $1.b }}" in step.body


@pytest.mark.asyncio
async def test_unbind_field_restores_original() -> None:
    ft = _filled()
    svc = _service([ft])
    chain = await svc.create_chain([], "Цепочка")
    step = await svc.add_step(chain.id, ft.id)

    await svc.bind_field(chain.id, step.id, location="/amount", ref_step=1, ref_path="x")
    await svc.unbind_field(chain.id, step.id, location="/amount")
    assert step.bindings == {}
    assert "{{" not in step.body
    assert "100" in step.body


@pytest.mark.asyncio
async def test_bind_field_rejects_bad_args_and_unknown_location() -> None:
    ft = _filled()
    svc = _service([ft])
    chain = await svc.create_chain([], "Цепочка")
    step = await svc.add_step(chain.id, ft.id)

    with pytest.raises(ValidationFailed):
        await svc.bind_field(chain.id, step.id, location="/amount", ref_step=0, ref_path="x")
    with pytest.raises(NotFoundError):
        await svc.bind_field(chain.id, step.id, location="/missing", ref_step=1, ref_path="x")
    # Unbinding a field that was never bound is a clean error.
    with pytest.raises(NotFoundError):
        await svc.unbind_field(chain.id, step.id, location="/amount")
