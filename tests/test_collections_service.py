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
