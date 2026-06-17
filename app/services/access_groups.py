"""Access-group CRUD and password unlock.

A group is a password-protected vault for sensitive test data (clients and the
filled templates produced from them). Knowing the password *is* membership —
there are no user accounts. Deletion is refused while any client or filled
template references the group, so a group can never be removed in a way that
silently exposes private data.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AccessGroup
from app.repositories.access_group import AccessGroupRepository
from app.schemas.access_group import AccessGroupCreate, AccessGroupUpdate
from app.utils.errors import IntegrityViolation, NotFoundError, ValidationFailed
from app.utils.password import hash_password, verify_password


class AccessGroupService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = AccessGroupRepository(session)

    async def list_all(self) -> list[AccessGroup]:
        return await self.repo.list_all()

    async def get(self, group_id: uuid.UUID) -> AccessGroup:
        group = await self.repo.get(group_id)
        if group is None:
            raise NotFoundError("Группа не найдена")
        return group

    async def create(self, data: AccessGroupCreate) -> AccessGroup:
        name = data.name.strip()
        if not name:
            raise ValidationFailed("Название группы не может быть пустым")
        if not data.password.strip():
            raise ValidationFailed("Пароль не может быть пустым")
        if await self.repo.get_by_name(name) is not None:
            raise ValidationFailed("Группа с таким именем уже существует")
        return await self.repo.add(
            AccessGroup(name=name, color=data.color, password_hash=hash_password(data.password))
        )

    async def update(self, group_id: uuid.UUID, data: AccessGroupUpdate) -> AccessGroup:
        group = await self.get(group_id)
        if data.name is not None:
            name = data.name.strip()
            if not name:
                raise ValidationFailed("Название группы не может быть пустым")
            existing = await self.repo.get_by_name(name)
            if existing is not None and existing.id != group.id:
                raise ValidationFailed("Группа с таким именем уже существует")
            group.name = name
        if data.color is not None:
            group.color = data.color
        # A blank password means "leave it unchanged" — only rehash when a real
        # new secret is supplied.
        if data.password is not None and data.password.strip():
            group.password_hash = hash_password(data.password)
        await self.session.flush()
        return group

    async def delete(self, group_id: uuid.UUID) -> None:
        group = await self.get(group_id)
        clients = await self.repo.count_clients(group_id)
        filled = await self.repo.count_filled(group_id)
        if clients or filled:
            raise IntegrityViolation(
                "Нельзя удалить группу: к ней привязаны данные "
                f"(клиентов: {clients}, заполненных шаблонов: {filled}). "
                "Сначала переназначьте или удалите их."
            )
        await self.repo.delete(group)

    async def unlock(self, password: str) -> AccessGroup | None:
        """Return the group whose password matches ``password`` (or ``None``).

        Iterates groups and compares in constant time per row — fine for the
        handful of groups an instance holds. A blank password never matches.
        """

        if not password:
            return None
        for group in await self.repo.list_all():
            if verify_password(password, group.password_hash):
                return group
        return None
