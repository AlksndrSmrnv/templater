from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db.models import Collection, MessageTemplate
from app.services.collections import CollectionService, build_folder_tree
from app.services.templates import TemplateService
from app.utils.errors import ValidationFailed

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "postman_sample.json"


class FakeSession:
    """Minimal AsyncSession stand-in: ``add`` assigns ids like a real flush
    would, so service code that reads ``collection.id`` after add works."""

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()  # type: ignore[attr-defined]
        self.added.append(obj)

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_import_postman_creates_collection_and_templates() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    session = FakeSession()
    summary = await CollectionService(session).import_postman(data)  # type: ignore[arg-type]

    collections = [o for o in session.added if isinstance(o, Collection)]
    templates = [o for o in session.added if isinstance(o, MessageTemplate)]
    assert len(collections) == 1
    assert len(templates) == 3
    assert summary.templates_created == 3
    assert summary.unparsable == 1  # the GET health check
    assert summary.name == "Demo Bank"

    collection = collections[0]
    assert all(t.collection_id == collection.id for t in templates)

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
async def test_import_postman_rejects_garbage() -> None:
    with pytest.raises(ValidationFailed):
        await CollectionService(FakeSession()).import_postman({"nope": 1})  # type: ignore[arg-type]


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
        self.by_id = {c.id: c for c in collections}

    async def get(self, collection_id: uuid.UUID) -> SimpleNamespace | None:
        return self.by_id.get(collection_id)


class _FakeTemplateRepo:
    def __init__(self, templates: list[SimpleNamespace]) -> None:
        self.templates = templates

    async def list_by_collection(self, collection_id: uuid.UUID) -> list[SimpleNamespace]:
        return [t for t in self.templates if t.collection_id == collection_id]

    async def get(self, template_id: uuid.UUID) -> SimpleNamespace | None:
        return next((t for t in self.templates if t.id == template_id), None)

    async def get_many(self, ids):  # type: ignore[no-untyped-def]
        wanted = set(ids)
        return [t for t in self.templates if t.id in wanted]


def _service(
    collections: list[SimpleNamespace], templates: list[SimpleNamespace]
) -> CollectionService:
    svc = CollectionService(FakeSession())  # type: ignore[arg-type]
    svc.repo = _FakeCollectionRepo(collections)  # type: ignore[assignment]
    svc.templates = _FakeTemplateRepo(templates)  # type: ignore[assignment]
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
