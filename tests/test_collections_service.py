from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db.models import Collection, MessageTemplate
from app.services.collections import (
    ROOT_FOLDERS_KEY,
    CollectionService,
    build_folder_tree,
)
from app.services.templates import TemplateService
from app.utils.errors import ValidationFailed

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "postman_sample.json"
INSOMNIA_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "insomnia_sample.json"


class FakeSession:
    """Minimal AsyncSession stand-in: ``add`` assigns ids like a real flush
    would, so service code that reads ``collection.id`` after add works.

    Models the real session's ``autoflush=False`` for folder placement:
    when wired to a ``_FakeTemplateRepo``, ``flush()`` refreshes the repo's
    "database" snapshot, and query-like repo methods read only that snapshot.
    """

    def __init__(self) -> None:
        self.added: list[object] = []
        self.template_repo: _FakeTemplateRepo | None = None

    def add(self, obj: object) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()  # type: ignore[attr-defined]
        self.added.append(obj)

    async def flush(self) -> None:
        if self.template_repo is not None:
            self.template_repo.sync_flushed()


@pytest.mark.asyncio
async def test_import_postman_creates_collection_and_templates() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    session = FakeSession()
    project_id = uuid.uuid4()
    summary = await CollectionService(session).import_collection(  # type: ignore[arg-type]
        data, project_id=project_id
    )

    collections = [o for o in session.added if isinstance(o, Collection)]
    templates = [o for o in session.added if isinstance(o, MessageTemplate)]
    assert len(collections) == 1
    assert len(templates) == 3
    assert summary.templates_created == 3
    assert summary.unparsable == 1  # the GET health check
    assert summary.name == "Demo Bank"

    collection = collections[0]
    assert all(t.collection_id == collection.id for t in templates)
    assert all(t.project_id == project_id for t in templates)

    a2a = next(t for t in templates if t.name == "A2A Transfer")
    assert a2a.folder_path == ["Transfers"]
    assert a2a.http_method == "POST"
    assert a2a.format == "json"
    assert a2a.llm_meta["import_status"] == "imported"
    rquid = next(h for h in a2a.headers if h["key"] == "RqUID")
    assert rquid["mode"] == "dynamic" and rquid["value"] == "{{rqUID}}"

    health = next(t for t in templates if t.name == "Health Check")
    assert health.llm_meta["import_status"] == "unparsed"
    assert health.content == ""
    assert health.display_order >= 0


@pytest.mark.asyncio
async def test_import_insomnia_creates_collection_and_templates() -> None:
    data = json.loads(INSOMNIA_FIXTURE.read_text(encoding="utf-8"))
    session = FakeSession()
    project_id = uuid.uuid4()
    summary = await CollectionService(session).import_collection(  # type: ignore[arg-type]
        data, project_id=project_id
    )

    collections = [o for o in session.added if isinstance(o, Collection)]
    templates = [o for o in session.added if isinstance(o, MessageTemplate)]
    assert len(collections) == 1
    assert collections[0].source == "insomnia"
    assert len(templates) == 3
    assert summary.unparsable == 1  # the GET health check
    assert summary.name == "Demo Bank"

    a2a = next(t for t in templates if t.name == "A2A Transfer")
    assert a2a.folder_path == ["Transfers"]
    assert a2a.http_method == "POST"
    assert a2a.format == "json"
    assert a2a.llm_meta["import_status"] == "imported"
    rquid = next(h for h in a2a.headers if h["key"] == "RqUID")
    assert rquid["mode"] == "dynamic" and rquid["value"] == "{{rqUID}}"

    # Nested folder paths are seeded onto the collection, incl. the prefix.
    assert ["Transfers"] in collections[0].folders
    assert ["Transfers", "Legacy"] in collections[0].folders


@pytest.mark.asyncio
async def test_import_collection_rejects_garbage() -> None:
    with pytest.raises(ValidationFailed):
        await CollectionService(FakeSession()).import_collection(  # type: ignore[arg-type]
            {"nope": 1}, project_id=uuid.uuid4()
        )


@pytest.mark.asyncio
async def test_analyze_and_persist_rejects_unparsable_body() -> None:
    template = MessageTemplate(
        name="bad",
        format="json",
        content="{not json",
        original_content="{not json",
        llm_meta={},
        placeholders=[],
        headers=[],
    )
    svc = TemplateService(FakeSession())  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed):
        await svc.analyze_and_persist(template, llm_service=None)


def _tpl(name: str, folder_path: list[str], order: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        folder_path=folder_path,
        display_order=order,
        created_at=datetime(2026, 5, 29, 12, 0),
    )


def test_build_folder_tree_nests_by_folder_path() -> None:
    templates = [
        _tpl("root-item", [], 0),
        _tpl("a", ["Transfers"], 1),
        _tpl("b", ["Transfers", "A2A"], 2),
    ]
    tree = build_folder_tree(templates)
    assert [t.name for t in tree["templates"]] == ["root-item"]
    assert "Transfers" in tree["folders"]
    transfers = tree["folders"]["Transfers"]
    assert [t.name for t in transfers["templates"]] == ["a"]
    assert "A2A" in transfers["folders"]
    assert [t.name for t in transfers["folders"]["A2A"]["templates"]] == ["b"]


def test_build_folder_tree_seeds_empty_extra_folders() -> None:
    tree = build_folder_tree([], extra_folders=[["Reports"], ["Reports", "Daily"]])
    assert "Reports" in tree["folders"]
    daily = tree["folders"]["Reports"]["folders"]["Daily"]
    assert daily["templates"] == [] and daily["folders"] == {}


# ---- folder editing ops -------------------------------------------------------


def _tpl_in(coll_id: uuid.UUID, name: str, folder_path: list[str], order: int) -> SimpleNamespace:
    t = _tpl(name, folder_path, order)
    t.collection_id = coll_id
    return t


class _FakeCollectionRepo:
    def __init__(self, collections: list[SimpleNamespace]) -> None:
        self.collections = collections
        self.by_id = {c.id: c for c in collections}

    async def list_all(self) -> list[SimpleNamespace]:
        return list(self.collections)

    async def get(self, collection_id: uuid.UUID) -> SimpleNamespace | None:
        return self.by_id.get(collection_id)


def _placement(t: SimpleNamespace) -> tuple[object, tuple[str, ...]]:
    return (getattr(t, "collection_id", None), tuple(t.folder_path or []))


class _FakeTemplateRepo:
    def __init__(self, templates: list[SimpleNamespace]) -> None:
        self.templates = templates
        self.sync_flushed()

    def sync_flushed(self) -> None:
        """Snapshot collection/folder placement as the "database" would see it
        after a flush — ``list_by_folder`` must not observe pending in-memory
        mutations (autoflush is off in app.db.session)."""

        self._flushed = {t.id: _placement(t) for t in self.templates}

    async def list_all(self) -> list[SimpleNamespace]:
        return list(self.templates)

    async def list_by_collection(self, collection_id: uuid.UUID) -> list[SimpleNamespace]:
        return [t for t in self.templates if t.collection_id == collection_id]

    async def list_ungrouped(self) -> list[SimpleNamespace]:
        return [t for t in self.templates if getattr(t, "collection_id", None) is None]

    async def get(self, template_id: uuid.UUID) -> SimpleNamespace | None:
        return next((t for t in self.templates if t.id == template_id), None)

    async def get_many(self, ids):  # type: ignore[no-untyped-def]
        wanted = set(ids)
        return [t for t in self.templates if t.id in wanted]

    async def list_by_folder(
        self, collection_id: uuid.UUID | None, folder_path: list[str]
    ) -> list[SimpleNamespace]:
        # Like the real SQL query, this sees only *flushed* placement — a
        # service that mutates collection_id/folder_path and queries without
        # flushing must fail here the same way it would against Postgres.
        key = (collection_id, tuple(folder_path))
        return [t for t in self.templates if self._flushed.get(t.id) == key]

    async def next_display_order(
        self, collection_id: uuid.UUID | None, folder_path: list[str]
    ) -> int:
        key = (collection_id, tuple(folder_path))
        orders = [t.display_order for t in self.templates if _placement(t) == key]
        return (max(orders) + 1) if orders else 0


class _FakeSettingsRepo:
    """Key/value stand-in for ``SettingsRepository`` backed by a plain dict."""

    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = dict(values or {})

    async def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    async def set(self, key: str, value: object) -> None:
        self.values[key] = value


def _service(
    collections: list[SimpleNamespace],
    templates: list[SimpleNamespace],
    *,
    settings: _FakeSettingsRepo | None = None,
) -> CollectionService:
    session = FakeSession()
    template_repo = _FakeTemplateRepo(templates)
    session.template_repo = template_repo  # flush() refreshes the snapshot
    svc = CollectionService(session)  # type: ignore[arg-type]
    svc.repo = _FakeCollectionRepo(collections)  # type: ignore[assignment]
    svc.templates = template_repo  # type: ignore[assignment]
    svc.settings = settings or _FakeSettingsRepo()  # type: ignore[assignment]
    return svc


@pytest.mark.asyncio
async def test_create_folder_appends_and_rejects_duplicate() -> None:
    coll = SimpleNamespace(id=uuid.uuid4(), folders=[])
    svc = _service([coll], [])

    path = await svc.create_folder(coll.id, [], "Reports")
    assert path == ["Reports"]
    assert ["Reports"] in coll.folders

    with pytest.raises(ValidationFailed):
        await svc.create_folder(coll.id, [], "Reports")
    with pytest.raises(ValidationFailed):
        await svc.create_folder(coll.id, [], "   ")


@pytest.mark.asyncio
async def test_folder_ops_reject_missing_target() -> None:
    coll = SimpleNamespace(id=uuid.uuid4(), folders=[["A"]])
    svc = _service([coll], [])

    with pytest.raises(ValidationFailed):
        await svc.create_folder(coll.id, ["Ghost"], "Child")  # parent missing
    with pytest.raises(ValidationFailed):
        await svc.rename_folder(coll.id, ["Ghost"], "X")  # path missing
    with pytest.raises(ValidationFailed):
        # No-op rename (same name) of a missing folder must still be rejected,
        # not silently reported as success.
        await svc.rename_folder(coll.id, ["Ghost"], "Ghost")
    with pytest.raises(ValidationFailed):
        await svc.delete_folder(coll.id, ["Ghost"])  # path missing


@pytest.mark.asyncio
async def test_move_request_ignores_non_sibling_ids_in_order() -> None:
    coll = SimpleNamespace(id=uuid.uuid4(), folders=[["A"], ["B"]])
    t1 = _tpl_in(coll.id, "t1", ["A"], 0)
    elsewhere = _tpl_in(coll.id, "elsewhere", ["B"], 7)
    svc = _service([coll], [t1, elsewhere])

    # A crafted order that includes a template living in another folder must not
    # renumber that template.
    await svc.move_request(t1.id, coll.id, ["A"], [t1.id, elsewhere.id])
    assert t1.display_order == 0
    assert elsewhere.display_order == 7  # untouched


@pytest.mark.asyncio
async def test_rename_folder_reprefixes_templates_and_folders() -> None:
    coll = SimpleNamespace(id=uuid.uuid4(), folders=[["Transfers"], ["Transfers", "A2A"]])
    inside = _tpl_in(coll.id, "b", ["Transfers", "A2A"], 0)
    outside = _tpl_in(coll.id, "x", ["Other"], 1)
    svc = _service([coll], [inside, outside])

    new_path = await svc.rename_folder(coll.id, ["Transfers"], "Payments")
    assert new_path == ["Payments"]
    assert inside.folder_path == ["Payments", "A2A"]
    assert outside.folder_path == ["Other"]
    assert ["Payments"] in coll.folders and ["Payments", "A2A"] in coll.folders


@pytest.mark.asyncio
async def test_delete_folder_only_when_empty() -> None:
    coll = SimpleNamespace(id=uuid.uuid4(), folders=[["Empty"], ["Full"]])
    full = _tpl_in(coll.id, "t", ["Full"], 0)
    svc = _service([coll], [full])

    await svc.delete_folder(coll.id, ["Empty"])
    assert ["Empty"] not in coll.folders

    with pytest.raises(ValidationFailed):
        await svc.delete_folder(coll.id, ["Full"])
    assert ["Full"] in coll.folders


@pytest.mark.asyncio
async def test_move_request_sets_placement_and_reorders() -> None:
    coll = SimpleNamespace(id=uuid.uuid4(), folders=[["A"], ["B"]])
    t1 = _tpl_in(coll.id, "t1", ["A"], 0)
    t2 = _tpl_in(coll.id, "t2", ["B"], 0)
    svc = _service([coll], [t1, t2])

    # Move t1 into folder B, ordered after t2.
    await svc.move_request(t1.id, coll.id, ["B"], [t2.id, t1.id])
    assert t1.collection_id == coll.id and t1.folder_path == ["B"]
    assert t2.display_order == 0 and t1.display_order == 1


@pytest.mark.asyncio
async def test_move_request_keeps_hidden_siblings_without_duplicate_order() -> None:
    # Search can hide part of a folder from the client: the DnD payload then
    # covers only the visible items. Hidden siblings must keep their slots and
    # the folder must end up renumbered without duplicate display_order.
    coll = SimpleNamespace(id=uuid.uuid4(), folders=[["F"]])
    hidden = _tpl_in(coll.id, "hidden", ["F"], 0)
    v1 = _tpl_in(coll.id, "v1", ["F"], 1)
    v2 = _tpl_in(coll.id, "v2", ["F"], 2)
    svc = _service([coll], [hidden, v1, v2])

    # The user sees only v1/v2 and drags v2 above v1.
    await svc.move_request(v2.id, coll.id, ["F"], [v2.id, v1.id])
    assert hidden.display_order == 0  # hidden slot preserved
    assert v2.display_order == 1 and v1.display_order == 2
    orders = [hidden.display_order, v1.display_order, v2.display_order]
    assert len(set(orders)) == len(orders), "display_order must stay unique"


@pytest.mark.asyncio
async def test_move_request_across_collections_and_to_ungrouped() -> None:
    src = SimpleNamespace(id=uuid.uuid4(), folders=[])
    dst = SimpleNamespace(id=uuid.uuid4(), folders=[["Inbox"]])
    t = _tpl_in(src.id, "t", [], 0)
    svc = _service([src, dst], [t])

    await svc.move_request(t.id, dst.id, ["Inbox"], [t.id])
    assert t.collection_id == dst.id and t.folder_path == ["Inbox"]

    # Drop into "Без коллекции": collection None, root folder.
    await svc.move_request(t.id, None, [], [t.id])
    assert t.collection_id is None and t.folder_path == []


# ---- root-level folders (collection_id=None) ----------------------------------


def _tpl_root(name: str, folder_path: list[str], order: int) -> SimpleNamespace:
    t = _tpl(name, folder_path, order)
    t.collection_id = None
    return t


@pytest.mark.asyncio
async def test_root_create_folder_persists_to_settings() -> None:
    settings = _FakeSettingsRepo()
    svc = _service([], [], settings=settings)

    path = await svc.create_folder(None, [], "Inbox")
    assert path == ["Inbox"]
    assert settings.values[ROOT_FOLDERS_KEY] == [["Inbox"]]

    # Subfolder under an existing root folder.
    await svc.create_folder(None, ["Inbox"], "Urgent")
    assert ["Inbox", "Urgent"] in settings.values[ROOT_FOLDERS_KEY]

    with pytest.raises(ValidationFailed):
        await svc.create_folder(None, [], "Inbox")  # duplicate
    with pytest.raises(ValidationFailed):
        await svc.create_folder(None, ["Ghost"], "x")  # parent missing


@pytest.mark.asyncio
async def test_root_rename_folder_reprefixes_ungrouped_templates() -> None:
    settings = _FakeSettingsRepo({ROOT_FOLDERS_KEY: [["Transfers"], ["Transfers", "A2A"]]})
    inside = _tpl_root("b", ["Transfers", "A2A"], 0)
    svc = _service([], [inside], settings=settings)

    new_path = await svc.rename_folder(None, ["Transfers"], "Payments")
    assert new_path == ["Payments"]
    assert inside.folder_path == ["Payments", "A2A"]
    assert settings.values[ROOT_FOLDERS_KEY] == [["Payments"], ["Payments", "A2A"]]


@pytest.mark.asyncio
async def test_root_delete_folder_only_when_empty() -> None:
    settings = _FakeSettingsRepo({ROOT_FOLDERS_KEY: [["Empty"], ["Full"]]})
    full = _tpl_root("t", ["Full"], 0)
    svc = _service([], [full], settings=settings)

    await svc.delete_folder(None, ["Empty"])
    assert settings.values[ROOT_FOLDERS_KEY] == [["Full"]]

    with pytest.raises(ValidationFailed):
        await svc.delete_folder(None, ["Full"])  # not empty
    assert settings.values[ROOT_FOLDERS_KEY] == [["Full"]]


@pytest.mark.asyncio
async def test_build_workspace_tree_splits_root_folders_and_loose() -> None:
    settings = _FakeSettingsRepo({ROOT_FOLDERS_KEY: [["Inbox"], ["Inbox", "Urgent"]]})
    in_folder = _tpl_root("grouped", ["Inbox"], 0)
    loose = _tpl_root("loose", [], 1)
    svc = _service([], [in_folder, loose], settings=settings)

    tree = await svc.build_workspace_tree()
    # Root folders (incl. the empty subfolder) surface at the top.
    assert "Inbox" in tree["root_tree"]["folders"]
    assert "Urgent" in tree["root_tree"]["folders"]["Inbox"]["folders"]
    assert [t.name for t in tree["root_tree"]["folders"]["Inbox"]["templates"]] == ["grouped"]
    assert tree["root_tree"]["templates"] == []
    # Only the truly loose template stays in "Без коллекции".
    assert [t.name for t in tree["ungrouped_tree"]["templates"]] == ["loose"]
    assert tree["ungrouped_count"] == 1
