from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.db.models import Project
from app.schemas.project import ProjectCreate, ProjectUpdate
from app.services.projects import (
    DEFAULT_PROJECT_COLOR,
    DEFAULT_PROJECT_NAME,
    ProjectService,
)
from app.utils.errors import IntegrityViolation, NotFoundError, ValidationFailed


class FakeSession:
    async def flush(self) -> None:
        return None


class FakeProjectRepository:
    """In-memory ProjectRepository double keyed by id and name."""

    def __init__(self) -> None:
        self.projects: dict[uuid.UUID, Project] = {}
        self.template_counts: dict[uuid.UUID, int] = {}
        self.deleted: list[Project] = []

    async def list_all(self) -> list[Project]:
        return sorted(self.projects.values(), key=lambda p: p.name)

    async def get(self, project_id: uuid.UUID) -> Project | None:
        return self.projects.get(project_id)

    async def get_by_name(self, name: str) -> Project | None:
        for project in self.projects.values():
            if project.name == name:
                return project
        return None

    async def add(self, project: Project) -> Project:
        if project.id is None:
            project.id = uuid.uuid4()
        self.projects[project.id] = project
        return project

    async def delete(self, project: Project) -> None:
        self.deleted.append(project)
        self.projects.pop(project.id, None)

    async def count_templates(self, project_id: uuid.UUID) -> int:
        return self.template_counts.get(project_id, 0)


def make_service() -> tuple[ProjectService, FakeProjectRepository]:
    service = ProjectService(cast(Any, FakeSession()))
    repo = FakeProjectRepository()
    service.repo = cast(Any, repo)
    return service, repo


def test_project_create_schema_rejects_bad_color() -> None:
    with pytest.raises(ValidationError):
        ProjectCreate(name="P", color="red")
    with pytest.raises(ValidationError):
        ProjectCreate(name="P", color="#12345")
    with pytest.raises(ValidationError):
        ProjectCreate(name="", color="#112233")
    assert ProjectCreate(name="P", color="#A1b2C3").color == "#A1b2C3"


@pytest.mark.asyncio
async def test_create_rejects_duplicate_name() -> None:
    service, repo = make_service()
    await service.create(ProjectCreate(name="Альфа", color="#112233"))
    with pytest.raises(ValidationFailed):
        await service.create(ProjectCreate(name="Альфа", color="#445566"))
    assert len(repo.projects) == 1


@pytest.mark.asyncio
async def test_create_strips_name_and_rejects_blank() -> None:
    service, _repo = make_service()
    project = await service.create(ProjectCreate(name="  Бета  ", color="#112233"))
    assert project.name == "Бета"
    with pytest.raises(ValidationFailed):
        await service.create(ProjectCreate(name="   ", color="#112233"))


@pytest.mark.asyncio
async def test_update_renames_and_rejects_collision() -> None:
    service, _repo = make_service()
    first = await service.create(ProjectCreate(name="Альфа", color="#112233"))
    second = await service.create(ProjectCreate(name="Бета", color="#445566"))

    updated = await service.update(second.id, ProjectUpdate(name="Гамма", color="#778899"))
    assert updated.name == "Гамма"
    assert updated.color == "#778899"

    # Renaming onto an existing name is refused; renaming to itself is fine.
    with pytest.raises(ValidationFailed):
        await service.update(second.id, ProjectUpdate(name="Альфа"))
    same = await service.update(first.id, ProjectUpdate(name="Альфа"))
    assert same.id == first.id


@pytest.mark.asyncio
async def test_get_missing_raises_not_found() -> None:
    service, _repo = make_service()
    with pytest.raises(NotFoundError):
        await service.get(uuid.uuid4())


@pytest.mark.asyncio
async def test_delete_refused_while_templates_reference_project() -> None:
    service, repo = make_service()
    project = await service.create(ProjectCreate(name="Занятый", color="#112233"))
    repo.template_counts[project.id] = 3

    with pytest.raises(IntegrityViolation) as exc_info:
        await service.delete(project.id)
    assert exc_info.value.status_code == 409
    assert "3" in exc_info.value.message
    assert project.id in repo.projects  # nothing deleted


@pytest.mark.asyncio
async def test_delete_succeeds_for_unused_project() -> None:
    service, repo = make_service()
    project = await service.create(ProjectCreate(name="Пустой", color="#112233"))
    await service.delete(project.id)
    assert repo.deleted == [project]
    assert project.id not in repo.projects


@pytest.mark.asyncio
async def test_get_or_create_by_name_reuses_and_creates() -> None:
    service, repo = make_service()
    existing = await service.create(ProjectCreate(name="Альфа", color="#112233"))

    reused = await service.get_or_create_by_name("Альфа", color="#FF0000")
    assert reused.id == existing.id
    assert reused.color == "#112233"  # existing color untouched

    created = await service.get_or_create_by_name("Новый", color="#FF0000")
    assert created.name == "Новый"
    assert created.color == "#FF0000"
    assert len(repo.projects) == 2

    # Blank name falls back to the service default project.
    fallback = await service.get_or_create_by_name("   ")
    assert fallback.name == DEFAULT_PROJECT_NAME
    assert fallback.color == DEFAULT_PROJECT_COLOR
