"""Project CRUD: user-defined tags (name + highlight color) for templates.

Every template belongs to exactly one project, so deletion is refused while
any template references the project (the RESTRICT FK is only a backstop).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Project
from app.repositories.project import ProjectRepository
from app.schemas.project import ProjectCreate, ProjectUpdate
from app.utils.errors import IntegrityViolation, NotFoundError, ValidationFailed

DEFAULT_PROJECT_NAME = "Без проекта"
DEFAULT_PROJECT_COLOR = "#9E9E9E"


class ProjectService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = ProjectRepository(session)

    async def list_all(self) -> list[Project]:
        return await self.repo.list_all()

    async def get(self, project_id: uuid.UUID) -> Project:
        project = await self.repo.get(project_id)
        if project is None:
            raise NotFoundError("Проект не найден")
        return project

    async def create(self, data: ProjectCreate) -> Project:
        name = data.name.strip()
        if not name:
            raise ValidationFailed("Название проекта не может быть пустым")
        if await self.repo.get_by_name(name) is not None:
            raise ValidationFailed("Проект с таким именем уже существует")
        return await self.repo.add(Project(name=name, color=data.color))

    async def update(self, project_id: uuid.UUID, data: ProjectUpdate) -> Project:
        project = await self.get(project_id)
        if data.name is not None:
            name = data.name.strip()
            if not name:
                raise ValidationFailed("Название проекта не может быть пустым")
            existing = await self.repo.get_by_name(name)
            if existing is not None and existing.id != project.id:
                raise ValidationFailed("Проект с таким именем уже существует")
            project.name = name
        if data.color is not None:
            project.color = data.color
        await self.session.flush()
        return project

    async def delete(self, project_id: uuid.UUID) -> None:
        project = await self.get(project_id)
        count = await self.repo.count_templates(project_id)
        if count > 0:
            raise IntegrityViolation(
                f"Нельзя удалить проект: к нему привязаны шаблоны ({count})"
            )
        await self.repo.delete(project)

    async def get_or_create_by_name(
        self, name: str, color: str = DEFAULT_PROJECT_COLOR
    ) -> Project:
        """Resolve a project by exact name, creating it when missing.

        Used by the export/import flow so a package restored on another
        instance lands in the same-named project.
        """

        cleaned = name.strip() or DEFAULT_PROJECT_NAME
        existing = await self.repo.get_by_name(cleaned)
        if existing is not None:
            return existing
        return await self.repo.add(Project(name=cleaned, color=color))
