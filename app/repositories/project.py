from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageTemplate, Project


class ProjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[Project]:
        stmt = select(Project).order_by(Project.name)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get(self, project_id: uuid.UUID) -> Project | None:
        return await self.session.get(Project, project_id)

    async def get_by_name(self, name: str) -> Project | None:
        stmt = select(Project).where(Project.name == name)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, project: Project) -> Project:
        self.session.add(project)
        await self.session.flush()
        return project

    async def delete(self, project: Project) -> None:
        await self.session.delete(project)

    async def count_templates(self, project_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(MessageTemplate)
            .where(MessageTemplate.project_id == project_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())
