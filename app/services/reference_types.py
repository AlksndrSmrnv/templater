from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DATA_ENTITY_TYPES, RESERVED_ENTITY_TYPES, ReferenceType
from app.repositories.reference_type import ReferenceTypeRepository
from app.schemas.attribute import AttributeDefinitionCreate
from app.services.attribute_schema import AttributeSchemaService
from app.utils.errors import IntegrityViolation, ValidationFailed

# Reference-type codes are reused verbatim as ``entity_type`` strings and as URL
# path segments, so keep them to a conservative snake_case identifier.
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass
class ColumnSpec:
    """One user-defined column of a new reference type."""

    name: str
    label: str
    data_type: str
    is_required: bool = False
    enum_values: list[str] = field(default_factory=list)


class ReferenceTypeService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = ReferenceTypeRepository(session)
        self.schema = AttributeSchemaService(session)

    async def list_all(self) -> list[ReferenceType]:
        return await self.repo.list_all()

    async def codes(self) -> list[str]:
        return await self.repo.codes()

    async def get(self, code: str) -> ReferenceType | None:
        return await self.repo.get(code)

    async def all_attr_entity_types(self) -> list[str]:
        """Every ``entity_type`` that may own attribute definitions."""

        return list(DATA_ENTITY_TYPES) + await self.repo.codes()

    async def create(
        self,
        *,
        code: str,
        title: str,
        icon: str,
        description: str,
        columns: list[ColumnSpec],
    ) -> ReferenceType:
        code = code.strip()
        title = title.strip()
        icon = icon.strip()
        description = description.strip()

        if not _CODE_RE.fullmatch(code):
            raise ValidationFailed(
                "Код справочника должен начинаться с латинской буквы и содержать только "
                "строчные латинские буквы, цифры и подчёркивание"
            )
        # Mirror the DB column limits so user input can't trip an IntegrityError
        # (which would surface as a 500) on flush.
        if len(code) > 64:
            raise ValidationFailed("Код справочника не может быть длиннее 64 символов")
        if code in RESERVED_ENTITY_TYPES:
            raise ValidationFailed(f"Код '{code}' зарезервирован и не может быть использован")
        if not title:
            raise ValidationFailed("Название справочника обязательно")
        if len(title) > 255:
            raise ValidationFailed("Название справочника не может быть длиннее 255 символов")
        if len(icon) > 16:
            raise ValidationFailed("Иконка не может быть длиннее 16 символов")
        if await self.repo.exists(code):
            raise IntegrityViolation(f"Справочник с кодом '{code}' уже существует")

        ref_type = ReferenceType(
            code=code,
            title=title,
            icon=icon,
            description=description,
            display_order=0,
        )
        try:
            await self.repo.add(ref_type)
        except IntegrityError as exc:
            await self.session.rollback()
            raise IntegrityViolation(f"Справочник с кодом '{code}' уже существует") from exc

        # Reuse AttributeSchemaService for per-column validation (name/type/uniqueness).
        for order, col in enumerate(columns, start=1):
            options: dict[str, Any] = {}
            if col.data_type == "enum":
                options = {"values": col.enum_values}
            await self.schema.create(
                AttributeDefinitionCreate(
                    entity_type=code,
                    name=col.name,
                    label=col.label or col.name,
                    data_type=col.data_type,
                    is_required=col.is_required,
                    display_order=order * 10,
                    description="",
                    options=options,
                )
            )
        return ref_type
