from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.db.models import HeaderPreset, MessageTemplate, Project
from app.schemas.header_preset import (
    HeaderPresetCreate,
    HeaderPresetUpdate,
    PresetHeaderIn,
)
from app.services.header_presets import (
    HeaderPresetService,
    header_mode,
    normalize_preset_headers,
)
from app.utils.errors import NotFoundError, ValidationFailed


class FakeSession:
    """Models autoflush=False: flush is an explicit no-op the service awaits."""

    async def flush(self) -> None:
        return None


class FakeHeaderPresetRepository:
    def __init__(self) -> None:
        self.presets: dict[uuid.UUID, HeaderPreset] = {}
        self.deleted: list[HeaderPreset] = []

    async def list_all(self) -> list[HeaderPreset]:
        return sorted(self.presets.values(), key=lambda p: p.name)

    async def list_by_project(self, project_id: uuid.UUID) -> list[HeaderPreset]:
        return sorted(
            (p for p in self.presets.values() if p.project_id == project_id),
            key=lambda p: p.name,
        )

    async def get(self, preset_id: uuid.UUID) -> HeaderPreset | None:
        return self.presets.get(preset_id)

    async def get_by_name_in_project(
        self, project_id: uuid.UUID, name: str
    ) -> HeaderPreset | None:
        for preset in self.presets.values():
            if preset.project_id == project_id and preset.name == name:
                return preset
        return None

    async def add(self, preset: HeaderPreset) -> HeaderPreset:
        if preset.id is None:
            preset.id = uuid.uuid4()
        self.presets[preset.id] = preset
        return preset

    async def delete(self, preset: HeaderPreset) -> None:
        self.deleted.append(preset)
        self.presets.pop(preset.id, None)


class FakeProjectRepository:
    def __init__(self) -> None:
        self.projects: dict[uuid.UUID, Project] = {}

    async def get(self, project_id: uuid.UUID) -> Project | None:
        return self.projects.get(project_id)


def make_service() -> tuple[HeaderPresetService, FakeHeaderPresetRepository, uuid.UUID, uuid.UUID]:
    service = HeaderPresetService(cast(Any, FakeSession()))
    repo = FakeHeaderPresetRepository()
    projects = FakeProjectRepository()
    service.repo = cast(Any, repo)
    service.projects = cast(Any, projects)
    pa, pb = uuid.uuid4(), uuid.uuid4()
    projects.projects[pa] = Project(id=pa, name="A", color="#111111")
    projects.projects[pb] = Project(id=pb, name="B", color="#222222")
    return service, repo, pa, pb


# --- header normalization -------------------------------------------------


def test_header_mode_detects_dynamic() -> None:
    assert header_mode("{{rquid}}") == "dynamic"
    assert header_mode("Bearer {{ token }}") == "dynamic"
    assert header_mode("application/json") == "literal"
    assert header_mode("") == "literal"


def test_normalize_preset_headers_shapes_rows_and_drops_blank_keys() -> None:
    rows = [
        PresetHeaderIn(key="RqUID", value="{{rquid}}"),
        PresetHeaderIn(key="Content-Type", value="application/json"),
        PresetHeaderIn(key="   ", value="ignored"),
    ]
    out = normalize_preset_headers(rows)
    assert [h["key"] for h in out] == ["RqUID", "Content-Type"]
    assert out[0] == {
        "key": "RqUID",
        "value": "{{rquid}}",
        "mode": "dynamic",
        "original": "{{rquid}}",
        "disabled": False,
    }
    assert out[1]["mode"] == "literal"


def test_normalize_preset_headers_accepts_raw_dicts() -> None:
    out = normalize_preset_headers([{"key": "X", "value": "{{operuid}}"}])
    assert out == [
        {"key": "X", "value": "{{operuid}}", "mode": "dynamic", "original": "{{operuid}}", "disabled": False}
    ]


# --- CRUD -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_normalizes_and_strips() -> None:
    service, _repo, pa, _pb = make_service()
    preset = await service.create(
        HeaderPresetCreate(
            name="  A2A  ",
            project_id=pa,
            url="  https://host/a2a  ",
            headers=[PresetHeaderIn(key="RqUID", value="{{rquid}}")],
        )
    )
    assert preset.name == "A2A"
    assert preset.url == "https://host/a2a"
    assert preset.headers[0]["mode"] == "dynamic"


@pytest.mark.asyncio
async def test_create_uppercases_http_method() -> None:
    service, _repo, pa, _pb = make_service()
    preset = await service.create(
        HeaderPresetCreate(name="A2A", project_id=pa, http_method="  post  ")
    )
    assert preset.http_method == "POST"


@pytest.mark.asyncio
async def test_update_changes_http_method() -> None:
    service, _repo, pa, _pb = make_service()
    preset = await service.create(
        HeaderPresetCreate(name="Std", project_id=pa, http_method="GET")
    )
    updated = await service.update(preset.id, HeaderPresetUpdate(http_method="put"))
    assert updated.http_method == "PUT"


@pytest.mark.asyncio
async def test_create_rejects_invalid_http_method() -> None:
    # The server mirrors the form's «Тип запроса» select — an out-of-list method
    # (e.g. from a crafted POST) is refused rather than stored unstyled.
    service, _repo, pa, _pb = make_service()
    with pytest.raises(ValidationFailed):
        await service.create(HeaderPresetCreate(name="X", project_id=pa, http_method="FOO"))


@pytest.mark.asyncio
async def test_create_rejects_unknown_project() -> None:
    service, _repo, _pa, _pb = make_service()
    with pytest.raises(ValidationFailed):
        await service.create(HeaderPresetCreate(name="X", project_id=uuid.uuid4()))


@pytest.mark.asyncio
async def test_name_unique_within_project_but_not_across() -> None:
    service, repo, pa, pb = make_service()
    await service.create(HeaderPresetCreate(name="Std", project_id=pa))
    with pytest.raises(ValidationFailed):
        await service.create(HeaderPresetCreate(name="Std", project_id=pa))
    # Same name under a different project is allowed.
    await service.create(HeaderPresetCreate(name="Std", project_id=pb))
    assert len(repo.presets) == 2


@pytest.mark.asyncio
async def test_update_renames_changes_project_and_replaces_headers() -> None:
    service, _repo, pa, pb = make_service()
    preset = await service.create(
        HeaderPresetCreate(
            name="Std", project_id=pa, headers=[PresetHeaderIn(key="A", value="1")]
        )
    )
    updated = await service.update(
        preset.id,
        HeaderPresetUpdate(
            name="Std2",
            project_id=pb,
            url="https://new",
            headers=[PresetHeaderIn(key="B", value="{{rquid}}")],
        ),
    )
    assert updated.name == "Std2"
    assert updated.project_id == pb
    assert updated.url == "https://new"
    assert [h["key"] for h in updated.headers] == ["B"]
    assert updated.headers[0]["mode"] == "dynamic"


@pytest.mark.asyncio
async def test_update_rejects_collision_in_target_project() -> None:
    service, _repo, pa, _pb = make_service()
    await service.create(HeaderPresetCreate(name="One", project_id=pa))
    two = await service.create(HeaderPresetCreate(name="Two", project_id=pa))
    with pytest.raises(ValidationFailed):
        await service.update(two.id, HeaderPresetUpdate(name="One"))


@pytest.mark.asyncio
async def test_get_missing_raises_not_found() -> None:
    service, _repo, _pa, _pb = make_service()
    with pytest.raises(NotFoundError):
        await service.get(uuid.uuid4())


@pytest.mark.asyncio
async def test_delete_removes_preset() -> None:
    service, repo, pa, _pb = make_service()
    preset = await service.create(HeaderPresetCreate(name="Std", project_id=pa))
    await service.delete(preset.id)
    assert repo.deleted == [preset]
    assert preset.id not in repo.presets


# --- apply to template ----------------------------------------------------


def test_apply_to_template_copies_url_and_headers_independently() -> None:
    pid = uuid.uuid4()
    preset = HeaderPreset(
        name="Std",
        url="https://host",
        headers=[{"key": "RqUID", "value": "{{rquid}}", "mode": "dynamic", "original": "{{rquid}}", "disabled": False}],
        project_id=pid,
    )
    template = MessageTemplate(project_id=pid)
    HeaderPresetService.apply_to_template(template, preset)
    assert template.url == "https://host"
    assert template.headers == preset.headers
    # Deep copy: mutating the template must not touch the preset.
    template.headers[0]["value"] = "changed"
    assert preset.headers[0]["value"] == "{{rquid}}"


def test_apply_to_template_replaces_http_method() -> None:
    pid = uuid.uuid4()
    preset = HeaderPreset(name="Std", url="https://host", headers=[], project_id=pid, http_method="PATCH")
    template = MessageTemplate(project_id=pid, http_method="GET")
    HeaderPresetService.apply_to_template(template, preset)
    assert template.http_method == "PATCH"


def test_apply_to_template_unset_preset_method_clears_template_method() -> None:
    # Replace semantics (like url/headers): a preset with no method clears the
    # template's method. Documented in apply_to_template's docstring.
    pid = uuid.uuid4()
    preset = HeaderPreset(name="Std", url="https://host", headers=[], project_id=pid, http_method="")
    template = MessageTemplate(project_id=pid, http_method="POST")
    HeaderPresetService.apply_to_template(template, preset)
    assert template.http_method == ""


def test_apply_to_template_rejects_preset_from_other_project() -> None:
    # Enforces the project tag even for a crafted request — the picker is
    # UI-filtered, but the server must refuse a mismatched preset too.
    preset = HeaderPreset(name="Std", url="https://host", headers=[], project_id=uuid.uuid4())
    template = MessageTemplate(project_id=uuid.uuid4())
    with pytest.raises(ValidationFailed):
        HeaderPresetService.apply_to_template(template, preset)
    # Nothing copied across.
    assert template.url != "https://host"


def test_create_schema_requires_project_id() -> None:
    with pytest.raises(ValidationError):
        HeaderPresetCreate(name="X")  # type: ignore[call-arg]
