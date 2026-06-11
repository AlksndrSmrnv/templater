"""Folder operations of FilledTemplateService (the «Заполненные шаблоны» tree).

Mirrors the conventions of ``test_collections_service.py``: SimpleNamespace
rows, fake repositories and a dict-backed settings repo — no live DB.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.services.filled_templates import (
    FILLED_ROOT_FOLDERS_KEY,
    FilledTemplateService,
)
from app.utils.errors import NotFoundError, ValidationFailed


class _FakeSession:
    async def flush(self) -> None:
        return None


class _FakeFilledRepo:
    def __init__(self, items: list[SimpleNamespace]) -> None:
        self.items = items

    async def list_all(self, *, search: str = "", limit: int = 200) -> list[SimpleNamespace]:
        term = search.strip().lower()
        rows = self.items
        if term:
            rows = [i for i in rows if term in i.name.lower()]
        return list(rows[:limit])

    async def get(self, filled_id: uuid.UUID) -> SimpleNamespace | None:
        return next((i for i in self.items if i.id == filled_id), None)

    async def get_many(self, ids):  # type: ignore[no-untyped-def]
        wanted = set(ids)
        return [i for i in self.items if i.id in wanted]


class _FakeSettingsRepo:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self.values = dict(values or {})

    async def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    async def set(self, key: str, value: object) -> None:
        self.values[key] = value


def _item(name: str, folder_path: list[str], order: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        folder_path=folder_path,
        display_order=order,
        created_at=datetime(2026, 6, 11, 12, 0),
    )


def _service(
    items: list[SimpleNamespace],
    *,
    settings: _FakeSettingsRepo | None = None,
) -> FilledTemplateService:
    svc = FilledTemplateService(cast(Any, _FakeSession()))
    svc.repo = _FakeFilledRepo(items)  # type: ignore[assignment]
    svc.settings = settings or _FakeSettingsRepo()  # type: ignore[assignment]
    return svc


# ---- create -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_folder_persists_to_settings_and_rejects_duplicate() -> None:
    settings = _FakeSettingsRepo()
    svc = _service([], settings=settings)

    path = await svc.create_folder([], "Проект")
    assert path == ["Проект"]
    assert settings.values[FILLED_ROOT_FOLDERS_KEY] == [["Проект"]]

    # Arbitrary nesting: subfolder under subfolder.
    await svc.create_folder(["Проект"], "Релиз")
    await svc.create_folder(["Проект", "Релиз"], "Фича")
    assert ["Проект", "Релиз", "Фича"] in settings.values[FILLED_ROOT_FOLDERS_KEY]

    with pytest.raises(ValidationFailed):
        await svc.create_folder([], "Проект")  # duplicate
    with pytest.raises(ValidationFailed):
        await svc.create_folder([], "   ")  # blank name
    with pytest.raises(ValidationFailed):
        await svc.create_folder(["Ghost"], "x")  # parent missing


@pytest.mark.asyncio
async def test_create_folder_accepts_parent_implied_by_items() -> None:
    # A folder that exists only via an item's folder_path is a valid parent.
    svc = _service([_item("a", ["Implied"], 0)])
    path = await svc.create_folder(["Implied"], "Child")
    assert path == ["Implied", "Child"]


# ---- rename -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_folder_reprefixes_items_and_explicit_folders() -> None:
    settings = _FakeSettingsRepo(
        {FILLED_ROOT_FOLDERS_KEY: [["Проект"], ["Проект", "Релиз"]]}
    )
    inside = _item("b", ["Проект", "Релиз"], 0)
    outside = _item("x", ["Другая"], 1)
    svc = _service([inside, outside], settings=settings)

    new_path = await svc.rename_folder(["Проект"], "Проект v2")
    assert new_path == ["Проект v2"]
    assert inside.folder_path == ["Проект v2", "Релиз"]
    assert outside.folder_path == ["Другая"]
    assert settings.values[FILLED_ROOT_FOLDERS_KEY] == [
        ["Проект v2"],
        ["Проект v2", "Релиз"],
    ]


@pytest.mark.asyncio
async def test_rename_folder_rejects_missing_and_collisions() -> None:
    settings = _FakeSettingsRepo({FILLED_ROOT_FOLDERS_KEY: [["A"], ["B"]]})
    svc = _service([], settings=settings)

    with pytest.raises(ValidationFailed):
        await svc.rename_folder(["Ghost"], "X")  # path missing
    with pytest.raises(ValidationFailed):
        # No-op rename (same name) of a missing folder must still be rejected,
        # not silently reported as success.
        await svc.rename_folder(["Ghost"], "Ghost")
    with pytest.raises(ValidationFailed):
        await svc.rename_folder(["A"], "B")  # collision with sibling
    with pytest.raises(ValidationFailed):
        await svc.rename_folder(["A"], "  ")  # blank name

    # Renaming to its own name is a no-op, not an error.
    assert await svc.rename_folder(["A"], "A") == ["A"]


# ---- delete -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_folder_only_when_empty() -> None:
    settings = _FakeSettingsRepo({FILLED_ROOT_FOLDERS_KEY: [["Empty"], ["Full"]]})
    full = _item("t", ["Full"], 0)
    svc = _service([full], settings=settings)

    await svc.delete_folder(["Empty"])
    assert settings.values[FILLED_ROOT_FOLDERS_KEY] == [["Full"]]

    with pytest.raises(ValidationFailed):
        await svc.delete_folder(["Full"])  # has an item
    assert ["Full"] in settings.values[FILLED_ROOT_FOLDERS_KEY]


@pytest.mark.asyncio
async def test_delete_folder_refuses_with_child_folders() -> None:
    settings = _FakeSettingsRepo(
        {FILLED_ROOT_FOLDERS_KEY: [["Parent"], ["Parent", "Child"]]}
    )
    svc = _service([], settings=settings)

    with pytest.raises(ValidationFailed):
        await svc.delete_folder(["Parent"])  # has a child folder
    with pytest.raises(ValidationFailed):
        await svc.delete_folder(["Ghost"])  # missing

    await svc.delete_folder(["Parent", "Child"])
    await svc.delete_folder(["Parent"])
    assert settings.values[FILLED_ROOT_FOLDERS_KEY] == []


# ---- move ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_filled_sets_folder_and_reorders_siblings() -> None:
    t1 = _item("t1", ["A"], 0)
    t2 = _item("t2", ["B"], 0)
    svc = _service([t1, t2])

    await svc.move_filled(t1.id, ["B"], [t2.id, t1.id])
    assert t1.folder_path == ["B"]
    assert t2.display_order == 0 and t1.display_order == 1


@pytest.mark.asyncio
async def test_move_filled_ignores_non_sibling_ids_in_order() -> None:
    t1 = _item("t1", ["A"], 0)
    elsewhere = _item("elsewhere", ["B"], 7)
    svc = _service([t1, elsewhere])

    # A crafted order including an item from another folder must not renumber it.
    await svc.move_filled(t1.id, ["A"], [t1.id, elsewhere.id])
    assert t1.display_order == 0
    assert elsewhere.display_order == 7  # untouched


@pytest.mark.asyncio
async def test_move_filled_to_root_and_missing_id() -> None:
    t = _item("t", ["A"], 3)
    svc = _service([t])

    await svc.move_filled(t.id, [], [t.id])
    assert t.folder_path == [] and t.display_order == 0

    with pytest.raises(NotFoundError):
        await svc.move_filled(uuid.uuid4(), [], [])


# ---- build_tree / list_folder_paths --------------------------------------------


@pytest.mark.asyncio
async def test_build_tree_nests_items_and_seeds_explicit_empty_folders() -> None:
    settings = _FakeSettingsRepo(
        {FILLED_ROOT_FOLDERS_KEY: [["Проект"], ["Проект", "Пустая"]]}
    )
    grouped = _item("grouped", ["Проект"], 0)
    loose = _item("loose", [], 1)
    svc = _service([grouped, loose], settings=settings)

    ctx = await svc.build_tree()
    tree = ctx["tree"]
    assert [t.name for t in tree["templates"]] == ["loose"]
    project = tree["folders"]["Проект"]
    assert [t.name for t in project["templates"]] == ["grouped"]
    assert "Пустая" in project["folders"]  # explicit empty folder seeded
    assert ctx["count"] == 2
    assert ctx["truncated"] is False


@pytest.mark.asyncio
async def test_build_tree_search_filters_items_and_drops_empty_folders() -> None:
    settings = _FakeSettingsRepo({FILLED_ROOT_FOLDERS_KEY: [["Пустая"]]})
    match = _item("Перевод A2A", ["Проект"], 0)
    other = _item("Выписка", [], 1)
    svc = _service([match, other], settings=settings)

    ctx = await svc.build_tree(search="перевод")
    tree = ctx["tree"]
    assert "Пустая" not in tree["folders"]  # empty folders not seeded in search
    assert [t.name for t in tree["folders"]["Проект"]["templates"]] == ["Перевод A2A"]
    assert tree["templates"] == []
    assert ctx["count"] == 1
    assert ctx["search"] == "перевод"


@pytest.mark.asyncio
async def test_list_folder_paths_merges_explicit_and_implied() -> None:
    settings = _FakeSettingsRepo({FILLED_ROOT_FOLDERS_KEY: [["Проект", "Релиз"]]})
    svc = _service([_item("a", ["Другая", "Фича"], 0)], settings=settings)

    paths = await svc.list_folder_paths()
    # Intermediate prefixes are included; output is sorted and unique.
    assert ["Проект"] in paths
    assert ["Проект", "Релиз"] in paths
    assert ["Другая"] in paths
    assert ["Другая", "Фича"] in paths
    assert paths == sorted(paths)
