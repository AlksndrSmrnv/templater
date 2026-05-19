from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import REFERENCE_TYPES, ReferenceValue
from app.repositories.attribute import AttributeDefinitionRepository
from app.repositories.entity import find_entities_referencing
from app.repositories.reference import ReferenceValueRepository
from app.schemas.reference import ReferenceValueCreate, ReferenceValueUpdate
from app.services.attribute_schema import AttributeSchemaService
from app.utils.errors import IntegrityViolation, NotFoundError, ValidationFailed


class ReferenceService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = ReferenceValueRepository(session)
        self.attrs = AttributeDefinitionRepository(session)
        self.schema = AttributeSchemaService(session)

    @staticmethod
    def _check_entity_type(entity_type: str) -> None:
        if entity_type not in REFERENCE_TYPES:
            raise ValidationFailed(f"Неизвестный тип справочника: {entity_type}")

    async def list(self, entity_type: str) -> list[ReferenceValue]:
        self._check_entity_type(entity_type)
        return await self.repo.list(entity_type)

    async def get(self, value_id: uuid.UUID) -> ReferenceValue:
        value = await self.repo.get(value_id)
        if value is None:
            raise NotFoundError("Запись справочника не найдена")
        return value

    async def create(self, data: ReferenceValueCreate) -> ReferenceValue:
        self._check_entity_type(data.entity_type)
        existing = await self.repo.get_by_code(data.entity_type, data.code)
        if existing is not None:
            raise IntegrityViolation(f"Код '{data.code}' уже занят в справочнике '{data.entity_type}'")
        attrs = await self.schema.validate_attributes(data.entity_type, data.attributes)
        value = ReferenceValue(
            entity_type=data.entity_type,
            code=data.code,
            name=data.name,
            description=data.description,
            attributes=attrs,
        )
        await self.repo.add(value)
        await self.session.commit()
        await self.session.refresh(value)
        return value

    async def update(self, value_id: uuid.UUID, data: ReferenceValueUpdate) -> ReferenceValue:
        value = await self.get(value_id)
        if data.code is not None and data.code != value.code:
            duplicate = await self.repo.get_by_code(value.entity_type, data.code)
            if duplicate is not None:
                raise IntegrityViolation(f"Код '{data.code}' уже занят")
            value.code = data.code
        if data.name is not None:
            value.name = data.name
        if data.description is not None:
            value.description = data.description
        if data.attributes is not None:
            value.attributes = await self.schema.validate_attributes(
                value.entity_type, data.attributes
            )
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(value)
        return value

    async def delete(self, value_id: uuid.UUID) -> None:
        value = await self.get(value_id)
        usage = await self._find_usage(value.entity_type, value.id)
        if usage:
            details = ", ".join(f"{k}: {v}" for k, v in usage.items())
            raise IntegrityViolation(
                f"Запись используется в данных ({details}). Удалите или измените зависимые объекты."
            )
        await self.repo.delete(value)
        await self.session.commit()

    async def _find_usage(self, ref_entity_type: str, target_id: uuid.UUID) -> dict[str, int]:
        # Build map of owner_entity_type -> list of attribute names that reference this ref type
        all_defs = await self.attrs.list_all()
        attrs_by_owner: dict[str, list[str]] = {}
        for d in all_defs:
            if d.data_type != "ref":
                continue
            if (d.options or {}).get("ref_entity") != ref_entity_type:
                continue
            if d.entity_type in ("client", "account", "card"):
                attrs_by_owner.setdefault(d.entity_type, []).append(d.name)
        if not attrs_by_owner:
            return {}
        return await find_entities_referencing(
            self.session,
            ref_entity_type=ref_entity_type,
            target_id=target_id,
            attribute_names_by_entity=attrs_by_owner,
        )
