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

from app.services import request_chain as rc_module
from app.services.filled_templates import _RoleIds
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
    async def get(self, chain_id: Any, *, visible_group_ids: Any = None) -> Any:
        chain = next((c for c in self.chains if c.id == chain_id), None)
        if chain is not None:
            chain.steps = sorted(
                (s for s in self.steps if s.chain_id == chain_id),
                key=lambda s: (s.position, s.created_at),
            )
        return chain

    async def list_all(self, *, limit: int = 200, visible_group_ids: Any = None) -> Any:
        return list(self.chains)

    async def list_folder_paths(self) -> Any:
        return [list(c.folder_path or []) for c in self.chains]

    async def next_display_order(self, folder_path: Any) -> Any:
        orders = [c.display_order for c in self.chains if list(c.folder_path or []) == list(folder_path)]
        return (max(orders) + 1) if orders else 0

    async def list_by_folder(self, folder_path: Any) -> Any:
        return sorted(
            (c for c in self.chains if list(c.folder_path or []) == list(folder_path)),
            key=lambda c: (c.display_order, c.created_at),
        )

    async def add(self, chain: Any) -> Any:
        if getattr(chain, "id", None) is None:
            chain.id = uuid.uuid4()
        # Real chains carry a server-default created_at; the in-memory model
        # leaves it None, so stamp one (move sorts siblings by it).
        if getattr(chain, "created_at", None) is None:
            chain.created_at = self._tick()
        self.chains.append(chain)
        return chain

    async def delete(self, chain: Any) -> None:
        self.chains = [c for c in self.chains if c.id != chain.id]
        self.steps = [s for s in self.steps if s.chain_id != chain.id]

    # steps
    async def get_step(self, step_id: Any) -> Any:
        return next((s for s in self.steps if s.id == step_id), None)

    async def next_position(self, chain_id: Any) -> Any:
        ps = [s.position for s in self.steps if s.chain_id == chain_id]
        return (max(ps) + 1) if ps else 0

    async def list_steps(self, chain_id: Any) -> Any:
        return sorted(
            (s for s in self.steps if s.chain_id == chain_id),
            key=lambda s: (s.position, s.created_at),
        )

    async def add_step(self, step: Any) -> Any:
        if getattr(step, "id", None) is None:
            step.id = uuid.uuid4()
        if getattr(step, "created_at", None) is None:
            step.created_at = self._tick()
        self.steps.append(step)
        return step

    async def delete_step(self, step: Any) -> None:
        self.steps = [s for s in self.steps if s.id != step.id]


class _FakeFilledRepo:
    def __init__(self, filled: list[SimpleNamespace]) -> None:
        self.filled = filled

    async def get(self, filled_id: Any, *, visible_group_ids: Any = None) -> Any:
        return next((f for f in self.filled if f.id == filled_id), None)

    async def list_folder_paths(self) -> Any:
        return [list(f.folder_path or []) for f in self.filled]

    async def list_by_folder(self, folder_path: Any) -> Any:
        return [f for f in self.filled if list(f.folder_path or []) == list(folder_path)]

    async def next_display_order(self, folder_path: Any) -> Any:
        orders = [
            f.display_order
            for f in self.filled
            if list(f.folder_path or []) == list(folder_path)
        ]
        return (max(orders) + 1) if orders else 0


class _FakeSettingsRepo:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = dict(values or {})

    async def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)


def _filled(
    *,
    name: str = "FT",
    group_id: uuid.UUID | None = None,
    group_name: str = "",
    project_name: str = "",
    project_color: str = "",
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
        # The real column is NOT NULL with default 0; next_display_order and
        # the unified append helper read it, so the fake carries it too.
        display_order=0,
        created_at=datetime(2026, 6, 25, 12, 0),
        group_id=group_id,
        group_name_snapshot=group_name,
        group_color_snapshot="#fff",
        project_name_snapshot=project_name,
        project_color_snapshot=project_color,
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
async def test_create_chain_appends_after_filled_templates_in_folder() -> None:
    # Shared per-folder display_order: a new chain must land after the folder's
    # filled templates, not at order 0 between them. Folder ["F"] holds two
    # templates (orders 0, 1) and no chains yet.
    t0 = _filled(name="t0")
    t0.folder_path = ["F"]
    t0.display_order = 0
    t0.created_at = datetime(2026, 6, 25, 12, 0)
    t1 = _filled(name="t1")
    t1.folder_path = ["F"]
    t1.display_order = 1
    t1.created_at = datetime(2026, 6, 25, 12, 1)
    svc = _service([t0, t1])

    chain = await svc.create_chain(["F"], "Новая цепочка")
    assert chain.folder_path == ["F"]
    assert chain.display_order == 2  # after both templates, not 0 between them


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
async def test_add_step_locks_project_then_rejects_other() -> None:
    a1 = _filled(name="a1", project_name="Платежи", project_color="#3366ff")
    a2 = _filled(name="a2", project_name="Платежи", project_color="#3366ff")
    b1 = _filled(name="b1", project_name="Выписки", project_color="#22aa22")
    svc = _service([a1, a2, b1])
    chain = await svc.create_chain([], "Цепочка")

    await svc.add_step(chain.id, a1.id)
    assert chain.project_name_snapshot == "Платежи"
    assert chain.project_color_snapshot == "#3366ff"

    await svc.add_step(chain.id, a2.id)  # same project — ok

    with pytest.raises(ValidationFailed):
        await svc.add_step(chain.id, b1.id)  # different project


@pytest.mark.asyncio
async def test_add_step_no_project_and_projected_cannot_mix() -> None:
    none1 = _filled(name="n1")  # project_name "" → no project
    proj = _filled(name="p1", project_name="Платежи")
    svc = _service([none1, proj])
    chain = await svc.create_chain([], "Цепочка")

    await svc.add_step(chain.id, none1.id)
    assert chain.project_name_snapshot == ""  # locked to «no project»

    with pytest.raises(ValidationFailed):
        await svc.add_step(chain.id, proj.id)  # projected can't join a no-project chain


@pytest.mark.asyncio
async def test_remove_last_step_clears_project_lock() -> None:
    a = _filled(name="a", project_name="Платежи", project_color="#3366ff")
    b = _filled(name="b", project_name="Выписки", project_color="#22aa22")
    svc = _service([a, b])
    chain = await svc.create_chain([], "Цепочка")

    step = await svc.add_step(chain.id, a.id)
    assert chain.project_name_snapshot == "Платежи"

    await svc.remove_step(chain.id, step.id)
    assert chain.project_name_snapshot == ""  # empty chain → lock dropped

    # A different project can now seed the (empty) chain.
    await svc.add_step(chain.id, b.id)
    assert chain.project_name_snapshot == "Выписки"


# ---- move_chain (mirrors FilledTemplateService.move_filled) --------------------


def _chain_ns(name: str, folder: list[str], order: int, created: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), name=name, folder_path=folder,
        display_order=order, created_at=created, steps=[],
    )


@pytest.mark.asyncio
async def test_move_chain_sets_folder_and_reorders_siblings() -> None:
    svc = _service()
    c1 = _chain_ns("c1", ["A"], 0, datetime(2026, 6, 25, 12, 0))
    c2 = _chain_ns("c2", ["B"], 0, datetime(2026, 6, 25, 12, 1))
    svc.repo.chains.extend([c1, c2])  # type: ignore[attr-defined]

    await svc.move_chain(c1.id, ["B"], [("c", c2.id), ("c", c1.id)])
    assert c1.folder_path == ["B"]
    assert c2.display_order == 0 and c1.display_order == 1


@pytest.mark.asyncio
async def test_move_chain_ignores_non_sibling_ids_in_order() -> None:
    svc = _service()
    c1 = _chain_ns("c1", ["A"], 0, datetime(2026, 6, 25, 12, 0))
    elsewhere = _chain_ns("elsewhere", ["B"], 7, datetime(2026, 6, 25, 12, 1))
    svc.repo.chains.extend([c1, elsewhere])  # type: ignore[attr-defined]

    # A crafted order including a chain from another folder must not renumber it.
    await svc.move_chain(c1.id, ["A"], [("c", c1.id), ("c", elsewhere.id)])
    assert c1.display_order == 0
    assert elsewhere.display_order == 7  # untouched


@pytest.mark.asyncio
async def test_move_chain_keeps_hidden_siblings_without_duplicate_order() -> None:
    # Search/truncation can hide part of a folder: the DnD payload then covers
    # only visible chains. Hidden siblings keep their slots and the folder ends
    # up renumbered without duplicates.
    svc = _service()
    hidden = _chain_ns("hidden", ["F"], 0, datetime(2026, 6, 25, 12, 0))
    v1 = _chain_ns("v1", ["F"], 1, datetime(2026, 6, 25, 12, 1))
    v2 = _chain_ns("v2", ["F"], 2, datetime(2026, 6, 25, 12, 2))
    svc.repo.chains.extend([hidden, v1, v2])  # type: ignore[attr-defined]

    # The user sees only v1/v2 and drags v2 above v1.
    await svc.move_chain(v2.id, ["F"], [("c", v2.id), ("c", v1.id)])
    assert hidden.display_order == 0  # hidden slot preserved
    assert v2.display_order == 1 and v1.display_order == 2
    orders = [hidden.display_order, v1.display_order, v2.display_order]
    assert len(set(orders)) == len(orders), "display_order must stay unique"


@pytest.mark.asyncio
async def test_move_chain_interleaves_with_filled_templates() -> None:
    # Unified per-folder order: moving a chain renumbers it among the folder's
    # filled templates too — the chain can land above a template, not only
    # below the template block.
    t0 = _filled(name="t0")
    t0.folder_path = ["F"]
    t0.display_order = 0
    t0.created_at = datetime(2026, 6, 25, 12, 0)
    t2 = _filled(name="t2")
    t2.folder_path = ["F"]
    t2.display_order = 2
    t2.created_at = datetime(2026, 6, 25, 12, 2)
    svc = _service([t0, t2])
    c1 = _chain_ns("c1", ["F"], 1, datetime(2026, 6, 25, 12, 1))
    svc.repo.chains.append(c1)  # type: ignore[attr-defined]

    # Drop the chain between the templates: order t0, c1, t2.
    await svc.move_chain(c1.id, ["F"], [("t", t0.id), ("c", c1.id), ("t", t2.id)])
    assert t0.display_order == 0
    assert c1.display_order == 1
    assert t2.display_order == 2
    orders = [t0.display_order, c1.display_order, t2.display_order]
    assert len(set(orders)) == len(orders), "display_order must stay unique"


@pytest.mark.asyncio
async def test_remove_step_renumbers_remaining() -> None:
    fts = [_filled(name=f"s{i}") for i in range(3)]
    svc = _service(fts)
    chain = await svc.create_chain([], "Цепочка")
    steps = [await svc.add_step(chain.id, f.id) for f in fts]

    await svc.remove_step(chain.id, steps[0].id)
    remaining = await svc.repo.list_steps(chain.id)
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
    ordered = await svc.repo.list_steps(chain.id)
    assert [s.name_snapshot for s in ordered] == ["s2", "s0", "s1"]
    assert [s.position for s in ordered] == [0, 1, 2]


# ---- bind / unbind ------------------------------------------------------------


async def _chain_with_two_steps() -> tuple[RequestChainService, Any, Any, Any]:
    """A chain whose second step (position 1) can reference the first. Both
    steps share the body '{"amount": 100}' with /amount marked filled."""

    fts = [_filled(name="Создать"), _filled(name="Подтвердить")]
    svc = _service(fts)
    chain = await svc.create_chain([], "Цепочка")
    s1 = await svc.add_step(chain.id, fts[0].id)
    s2 = await svc.add_step(chain.id, fts[1].id)
    return svc, chain, s1, s2


@pytest.mark.asyncio
async def test_bind_field_replaces_leaf_and_buffers_typed_original() -> None:
    svc, chain, _s1, s2 = await _chain_with_two_steps()

    await svc.bind_field(
        chain.id, s2.id, location="/amount", ref_step=1, ref_path="transferId"
    )
    # The reference lives inline in the body…
    assert "{{ $1.transferId }}" in s2.body
    # …and the original number is buffered (typed) for «Сбросить».
    assert s2.bindings == {"/amount": 100}


@pytest.mark.asyncio
async def test_bind_field_rebind_keeps_first_original() -> None:
    svc, chain, _s1, s2 = await _chain_with_two_steps()

    await svc.bind_field(chain.id, s2.id, location="/amount", ref_step=1, ref_path="a")
    await svc.bind_field(chain.id, s2.id, location="/amount", ref_step=1, ref_path="b")
    # Re-binding must not overwrite the buffered literal with the prior token.
    assert s2.bindings == {"/amount": 100}
    assert "{{ $1.b }}" in s2.body


@pytest.mark.asyncio
async def test_unbind_field_restores_number_type() -> None:
    svc, chain, _s1, s2 = await _chain_with_two_steps()

    await svc.bind_field(chain.id, s2.id, location="/amount", ref_step=1, ref_path="x")
    await svc.unbind_field(chain.id, s2.id, location="/amount")
    assert s2.bindings == {}
    assert "{{" not in s2.body
    # Restored as a JSON number, not the string "100".
    assert '"amount": 100' in s2.body
    assert '"100"' not in s2.body


@pytest.mark.asyncio
async def test_bind_field_rejects_forward_self_and_zero_reference() -> None:
    svc, chain, _s1, s2 = await _chain_with_two_steps()

    # ref_step must be a real *earlier* step (1 ≤ ref_step ≤ position).
    with pytest.raises(ValidationFailed):
        await svc.bind_field(chain.id, s2.id, location="/amount", ref_step=0, ref_path="x")
    with pytest.raises(ValidationFailed):
        await svc.bind_field(chain.id, s2.id, location="/amount", ref_step=2, ref_path="x")
    # The first step has no earlier step to reference at all.
    with pytest.raises(ValidationFailed):
        await svc.bind_field(chain.id, _s1.id, location="/amount", ref_step=1, ref_path="x")


@pytest.mark.asyncio
async def test_bind_field_unknown_location_and_unbind_unbound() -> None:
    svc, chain, _s1, s2 = await _chain_with_two_steps()

    with pytest.raises(NotFoundError):
        await svc.bind_field(chain.id, s2.id, location="/missing", ref_step=1, ref_path="x")
    # Unbinding a field that was never bound is a clean error.
    with pytest.raises(NotFoundError):
        await svc.unbind_field(chain.id, s2.id, location="/amount")


@pytest.mark.asyncio
async def test_bind_field_rejects_root_scalar() -> None:
    # A bare-scalar body has no replaceable field — walker can't set the root,
    # so binding the root must be rejected, not buffered-then-no-op'd.
    ft1 = _filled(name="a")
    ft2 = _filled(name="b")
    ft2.filled_content = "100"
    ft2.changed_locations = []
    svc = _service([ft1, ft2])
    chain = await svc.create_chain([], "Цепочка")
    await svc.add_step(chain.id, ft1.id)
    s2 = await svc.add_step(chain.id, ft2.id)

    for loc in ("/", ""):
        with pytest.raises(NotFoundError):
            await svc.bind_field(chain.id, s2.id, location=loc, ref_step=1, ref_path="x")
    assert s2.bindings == {}


# ---- client switching ---------------------------------------------------------


def _filled_with_role(
    *, client_id: uuid.UUID, mtid: uuid.UUID | None = None
) -> SimpleNamespace:
    ft = _filled(name="Создать перевод")
    ft.sender_client_id = client_id
    ft.sender_account_id = None
    ft.sender_card_id = None
    ft.receiver_client_id = None
    ft.receiver_account_id = None
    ft.receiver_card_id = None
    ft.account_owner_client_id = None
    ft.account_owner_account_id = None
    ft.account_owner_card_id = None
    ft.role_labels_snapshot = {"sender": "Иванов"}
    ft.message_template_id = mtid
    ft.template_name_snapshot = "Перевод"
    return ft


def _patch_rerender(monkeypatch: Any, *, body: str = '{"new": 1}', changed: list[str] | None = None) -> None:
    """Stub the re-render collaborators so the step body comes from a fake fill
    and labels/name don't hit the DB."""

    class _FakeTemplateService:
        def __init__(self, session: Any) -> None:
            pass

        async def get(self, tid: Any) -> SimpleNamespace:
            return SimpleNamespace(id=tid, format="json")

    class _FakeFiller:
        def __init__(self, session: Any) -> None:
            pass

        async def fill_template(self, template: Any, **ids: Any) -> tuple[str, list[str], list[str]]:
            return body, [], list(changed or ["/new"])

    async def _labels(session: Any, req: Any) -> dict[str, str]:
        return {"sender": "Сидоров"}

    async def _bits(session: Any, req: Any) -> dict[str, tuple[str, str]]:
        return {"sender": ("Сидоров", "ACC-9")}

    async def _groups(session: Any, req: Any) -> dict[uuid.UUID, Any]:
        return {}  # public by default — no cross-group conflict

    monkeypatch.setattr(rc_module, "TemplateService", _FakeTemplateService)
    monkeypatch.setattr(rc_module, "PlaceholderFiller", _FakeFiller)
    monkeypatch.setattr(rc_module, "collect_role_labels", _labels)
    monkeypatch.setattr(rc_module, "collect_role_short_bits", _bits)
    monkeypatch.setattr(rc_module, "collect_request_groups", _groups)


@pytest.mark.asyncio
async def test_add_step_copies_role_ids_and_labels() -> None:
    cid = uuid.uuid4()
    ft = _filled_with_role(client_id=cid, mtid=uuid.uuid4())
    svc = _service([ft])
    chain = await svc.create_chain([], "Цепочка")
    step = await svc.add_step(chain.id, ft.id)
    assert step.sender_client_id == cid
    assert step.role_labels_snapshot == {"sender": "Иванов"}


@pytest.mark.asyncio
async def test_switch_step_client_regenerates_body(monkeypatch: Any) -> None:
    cid, new_cid = uuid.uuid4(), uuid.uuid4()
    ft = _filled_with_role(client_id=cid, mtid=uuid.uuid4())
    svc = _service([ft])
    chain = await svc.create_chain([], "Цепочка")
    step = await svc.add_step(chain.id, ft.id)
    _patch_rerender(monkeypatch)

    returned, regenerated = await svc.switch_step_client(
        chain.id, step.id, "sender", _RoleIds(new_cid, None, None)
    )
    assert regenerated is True
    assert returned.body == '{"new": 1}'
    assert returned.sender_client_id == new_cid
    assert returned.changed_locations == ["/new"]
    assert returned.role_labels_snapshot == {"sender": "Сидоров"}
    assert "Сидоров" in returned.name_snapshot


@pytest.mark.asyncio
async def test_switch_step_client_without_source_template_keeps_body(monkeypatch: Any) -> None:
    cid, new_cid = uuid.uuid4(), uuid.uuid4()
    ft = _filled_with_role(client_id=cid, mtid=None)  # source template deleted
    svc = _service([ft])
    chain = await svc.create_chain([], "Цепочка")
    step = await svc.add_step(chain.id, ft.id)
    original_body = step.body
    _patch_rerender(monkeypatch)

    returned, regenerated = await svc.switch_step_client(
        chain.id, step.id, "sender", _RoleIds(new_cid, None, None)
    )
    assert regenerated is False
    assert returned.body == original_body  # not re-rendered
    assert returned.sender_client_id == new_cid  # roles still updated
    assert returned.role_labels_snapshot == {"sender": "Сидоров"}


@pytest.mark.asyncio
async def test_switch_step_preserves_surviving_binding(monkeypatch: Any) -> None:
    cid, new_cid = uuid.uuid4(), uuid.uuid4()
    ft = _filled_with_role(client_id=cid, mtid=uuid.uuid4())
    svc = _service([ft])
    chain = await svc.create_chain([], "Цепочка")
    await svc.add_step(chain.id, ft.id)
    svc.filled.filled.append(ft)  # type: ignore[attr-defined]
    s2 = await svc.add_step(chain.id, ft.id)
    # Bind /amount on the 2nd step to step 1's response, then switch its client.
    await svc.bind_field(chain.id, s2.id, location="/amount", ref_step=1, ref_path="transferId")
    assert "{{ $1.transferId }}" in s2.body
    # Re-render keeps the leaf /amount present so the reference must survive.
    _patch_rerender(monkeypatch, body='{"amount": 999}', changed=["/amount"])

    await svc.switch_step_client(chain.id, s2.id, "sender", _RoleIds(new_cid, None, None))
    assert "{{ $1.transferId }}" in s2.body  # binding re-applied
    assert s2.bindings.get("/amount") == 999  # reset buffer refreshed to new literal


@pytest.mark.asyncio
async def test_replace_role_everywhere_touches_only_steps_with_role(monkeypatch: Any) -> None:
    c_a, c_b, new_cid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ft_a = _filled_with_role(client_id=c_a, mtid=uuid.uuid4())  # has a sender
    ft_b = _filled_with_role(client_id=c_b, mtid=uuid.uuid4())
    ft_b.sender_client_id = None  # this step's template has no sender role
    ft_b.receiver_client_id = c_b
    svc = _service([ft_a, ft_b])
    chain = await svc.create_chain([], "Цепочка")
    sa = await svc.add_step(chain.id, ft_a.id)
    sb = await svc.add_step(chain.id, ft_b.id)
    _patch_rerender(monkeypatch)

    changed, regenerated = await svc.replace_role_everywhere(
        chain.id, "sender", _RoleIds(new_cid, None, None)
    )
    assert [s.id for s in changed] == [sa.id]
    assert regenerated == 1
    assert sa.sender_client_id == new_cid
    assert sb.sender_client_id is None  # step without the role is untouched


@pytest.mark.asyncio
async def test_replace_role_everywhere_updates_all_steps_with_role(monkeypatch: Any) -> None:
    c_a, c_b, new_cid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ft_a = _filled_with_role(client_id=c_a, mtid=uuid.uuid4())
    ft_b = _filled_with_role(client_id=c_b, mtid=uuid.uuid4())
    svc = _service([ft_a, ft_b])
    chain = await svc.create_chain([], "Цепочка")
    sa = await svc.add_step(chain.id, ft_a.id)
    sb = await svc.add_step(chain.id, ft_b.id)
    _patch_rerender(monkeypatch)

    changed, regenerated = await svc.replace_role_everywhere(
        chain.id, "sender", _RoleIds(new_cid, None, None)
    )
    assert {s.id for s in changed} == {sa.id, sb.id}
    assert regenerated == 2
    assert sa.sender_client_id == new_cid
    assert sb.sender_client_id == new_cid  # both senders set to the new client


@pytest.mark.asyncio
async def test_replace_role_everywhere_counts_only_regenerated(monkeypatch: Any) -> None:
    # A step whose source template was deleted has its role updated but body NOT
    # regenerated — it counts in ``changed`` but not in the regenerated count.
    c_a, c_b, new_cid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ft_a = _filled_with_role(client_id=c_a, mtid=uuid.uuid4())
    ft_b = _filled_with_role(client_id=c_b, mtid=None)  # source template gone
    svc = _service([ft_a, ft_b])
    chain = await svc.create_chain([], "Цепочка")
    sa = await svc.add_step(chain.id, ft_a.id)
    sb = await svc.add_step(chain.id, ft_b.id)
    _patch_rerender(monkeypatch)

    changed, regenerated = await svc.replace_role_everywhere(
        chain.id, "sender", _RoleIds(new_cid, None, None)
    )
    assert {s.id for s in changed} == {sa.id, sb.id}  # both role-updated
    assert regenerated == 1  # only the one with a live source template


@pytest.mark.asyncio
async def test_replace_role_everywhere_requires_a_client(monkeypatch: Any) -> None:
    ft = _filled_with_role(client_id=uuid.uuid4(), mtid=uuid.uuid4())
    svc = _service([ft])
    chain = await svc.create_chain([], "Цепочка")
    await svc.add_step(chain.id, ft.id)
    _patch_rerender(monkeypatch)
    with pytest.raises(ValidationFailed):
        await svc.replace_role_everywhere(chain.id, "sender", _RoleIds(None, None, None))


@pytest.mark.asyncio
async def test_replace_role_everywhere_rejects_cross_group(monkeypatch: Any) -> None:
    # Two steps in group A; replacing the sender role with a group-B client must
    # be rejected so the chain's single group_id can't hide B's data from A.
    g_a, g_b = uuid.uuid4(), uuid.uuid4()
    c_a, c_b = uuid.uuid4(), uuid.uuid4()
    ft1 = _filled_with_role(client_id=c_a, mtid=uuid.uuid4())
    ft2 = _filled_with_role(client_id=c_a, mtid=uuid.uuid4())
    ft2.receiver_client_id = c_a  # a second role keeps the chain anchored in A
    svc = _service([ft1, ft2])
    chain = await svc.create_chain([], "Цепочка")
    await svc.add_step(chain.id, ft1.id)
    await svc.add_step(chain.id, ft2.id)
    _patch_rerender(monkeypatch)

    group_of = {
        c_a: {g_a: SimpleNamespace(name="A", color="#a")},
        c_b: {g_b: SimpleNamespace(name="B", color="#b")},
    }

    async def _groups(session: Any, req: Any) -> dict[uuid.UUID, Any]:
        out: dict[uuid.UUID, Any] = {}
        for cid in (req.sender_client_id, req.receiver_client_id):
            out.update(group_of.get(cid, {}))
        return out

    monkeypatch.setattr(rc_module, "collect_request_groups", _groups)

    with pytest.raises(ValidationFailed):
        await svc.replace_role_everywhere(chain.id, "sender", _RoleIds(c_b, None, None))


@pytest.mark.asyncio
async def test_switch_step_client_requires_a_client(monkeypatch: Any) -> None:
    ft = _filled_with_role(client_id=uuid.uuid4(), mtid=uuid.uuid4())
    svc = _service([ft])
    chain = await svc.create_chain([], "Цепочка")
    step = await svc.add_step(chain.id, ft.id)
    _patch_rerender(monkeypatch)
    with pytest.raises(ValidationFailed):
        await svc.switch_step_client(chain.id, step.id, "sender", _RoleIds(None, None, None))


@pytest.mark.asyncio
async def test_switch_step_client_rejects_cross_group(monkeypatch: Any) -> None:
    # Two steps start in group A; switching one step's client into group B must
    # be rejected so the chain's single group_id can't hide B's data from A.
    g_a, g_b = uuid.uuid4(), uuid.uuid4()
    c_a, c_b = uuid.uuid4(), uuid.uuid4()
    ft1 = _filled_with_role(client_id=c_a, mtid=uuid.uuid4())
    ft2 = _filled_with_role(client_id=c_a, mtid=uuid.uuid4())
    svc = _service([ft1, ft2])
    chain = await svc.create_chain([], "Цепочка")
    await svc.add_step(chain.id, ft1.id)
    svc.filled.filled.append(ft2)  # type: ignore[attr-defined]
    s2 = await svc.add_step(chain.id, ft2.id)

    _patch_rerender(monkeypatch)
    group_of = {
        c_a: {g_a: SimpleNamespace(name="A", color="#a")},
        c_b: {g_b: SimpleNamespace(name="B", color="#b")},
    }

    async def _groups(session: Any, req: Any) -> dict[uuid.UUID, Any]:
        return dict(group_of.get(req.sender_client_id, {}))

    monkeypatch.setattr(rc_module, "collect_request_groups", _groups)

    with pytest.raises(ValidationFailed):
        await svc.switch_step_client(chain.id, s2.id, "sender", _RoleIds(c_b, None, None))
