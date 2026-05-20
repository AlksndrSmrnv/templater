from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ALL_ATTR_ENTITY_TYPES,
    REFERENCE_TYPES,
    AttributeDefinition,
)
from app.repositories.attribute import AttributeDefinitionRepository
from app.repositories.reference import ReferenceValueRepository
from app.schemas.attribute import (
    ALLOWED_TYPES,
    AttributeDefinitionCreate,
    AttributeDefinitionUpdate,
)
from app.utils.errors import IntegrityViolation, NotFoundError, ValidationFailed


class AttributeSchemaService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.attrs = AttributeDefinitionRepository(session)
        self.refs = ReferenceValueRepository(session)

    async def list_schema(self, entity_type: str, *, include_deprecated: bool = False) -> list[AttributeDefinition]:
        self._check_entity_type(entity_type)
        return await self.attrs.list_by_entity(entity_type, include_deprecated=include_deprecated)

    async def list_all(self) -> list[AttributeDefinition]:
        return await self.attrs.list_all()

    async def create(self, data: AttributeDefinitionCreate) -> AttributeDefinition:
        self._check_entity_type(data.entity_type)
        if data.data_type not in ALLOWED_TYPES:
            raise ValidationFailed(f"Неизвестный тип атрибута: {data.data_type}")
        if data.data_type == "ref":
            ref = (data.options or {}).get("ref_entity")
            if ref not in REFERENCE_TYPES:
                raise ValidationFailed(
                    "Для атрибута типа 'ref' нужно указать options.ref_entity из справочников"
                )
        if data.data_type == "enum":
            values = (data.options or {}).get("values")
            if not isinstance(values, list) or not values:
                raise ValidationFailed("Для атрибута типа 'enum' нужно указать options.values")

        existing = await self.attrs.get_by_name(data.entity_type, data.name)
        if existing is not None:
            raise IntegrityViolation(
                f"Атрибут '{data.name}' уже существует для типа '{data.entity_type}'"
            )
        attr = AttributeDefinition(
            entity_type=data.entity_type,
            name=data.name,
            label=data.label,
            data_type=data.data_type,
            is_required=data.is_required,
            is_deprecated=data.is_deprecated,
            display_order=data.display_order,
            description=data.description,
            options=data.options or {},
        )
        try:
            await self.attrs.add(attr)
        except IntegrityError as exc:
            await self.session.rollback()
            raise IntegrityViolation(
                f"Атрибут '{data.name}' уже существует для типа '{data.entity_type}'"
            ) from exc
        return attr

    async def update(self, attr_id: uuid.UUID, data: AttributeDefinitionUpdate) -> AttributeDefinition:
        attr = await self.attrs.get_by_id(attr_id)
        if attr is None:
            raise NotFoundError("Атрибут не найден")
        if data.label is not None:
            attr.label = data.label
        if data.is_required is not None:
            attr.is_required = data.is_required
        if data.is_deprecated is not None:
            attr.is_deprecated = data.is_deprecated
        if data.display_order is not None:
            attr.display_order = data.display_order
        if data.description is not None:
            attr.description = data.description
        if data.options is not None:
            attr.options = data.options
        await self.session.flush()
        return attr

    async def deprecate(self, attr_id: uuid.UUID) -> AttributeDefinition:
        return await self.update(attr_id, AttributeDefinitionUpdate(is_deprecated=True))

    @staticmethod
    def _check_entity_type(entity_type: str) -> None:
        if entity_type not in ALL_ATTR_ENTITY_TYPES:
            raise ValidationFailed(f"Неизвестный тип сущности: {entity_type}")

    async def validate_attributes(
        self,
        entity_type: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate ``values`` against active attribute_definitions and return normalized dict.

        - Required attributes must be present (non-empty).
        - Type-cast values when possible (date strings → date, numbers, bools, enum values).
        - Unknown attribute names are kept verbatim (so legacy values persist), but a warning
          could be added later if needed.
        """

        definitions = await self.attrs.list_by_entity(entity_type, include_deprecated=False)
        defs_by_name = {d.name: d for d in definitions}
        normalized: dict[str, Any] = {}
        errors: list[str] = []

        for d in definitions:
            raw = values.get(d.name)
            if raw is None or raw == "":
                if d.is_required:
                    errors.append(f"Поле '{d.label}' обязательно")
                continue
            try:
                normalized[d.name] = await self._normalize_value(d, raw)
            except ValidationFailed as exc:
                errors.append(f"{d.label}: {exc.message}")

        # Preserve any existing keys that aren't in active schema (legacy/deprecated):
        for k, v in values.items():
            if k not in defs_by_name and v not in (None, ""):
                normalized[k] = v

        if errors:
            raise ValidationFailed("Проверка атрибутов не пройдена", details=errors)
        return normalized

    async def _normalize_value(self, d: AttributeDefinition, value: Any) -> Any:
        dt = d.data_type
        if dt in ("string", "text"):
            return str(value)
        if dt == "int":
            try:
                return int(value)
            except (TypeError, ValueError):
                raise ValidationFailed("ожидается целое число")
        if dt == "number":
            try:
                return float(value)
            except (TypeError, ValueError):
                raise ValidationFailed("ожидается число")
        if dt == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in ("1", "true", "yes", "on")
            return bool(value)
        if dt == "date":
            if isinstance(value, date):
                return value.isoformat()
            try:
                return date.fromisoformat(str(value)).isoformat()
            except ValueError:
                raise ValidationFailed("ожидается дата ISO (YYYY-MM-DD)")
        if dt == "datetime":
            if isinstance(value, datetime):
                return value.isoformat()
            try:
                return datetime.fromisoformat(str(value)).isoformat()
            except ValueError:
                raise ValidationFailed("ожидается дата-время ISO")
        if dt == "enum":
            allowed = (d.options or {}).get("values", [])
            if value not in allowed:
                raise ValidationFailed(f"должно быть одним из: {', '.join(map(str, allowed))}")
            return value
        if dt == "ref":
            ref_entity = (d.options or {}).get("ref_entity")
            if not ref_entity:
                raise ValidationFailed("отсутствует ref_entity в схеме атрибута")
            try:
                ref_id = uuid.UUID(str(value))
            except ValueError:
                raise ValidationFailed("ожидается UUID ссылки на справочник")
            if not await self.refs.exists(ref_entity, ref_id):
                raise ValidationFailed(f"значение не найдено в справочнике '{ref_entity}'")
            return str(ref_id)
        return value
