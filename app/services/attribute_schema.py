from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DATA_ENTITY_TYPES, AttributeDefinition
from app.repositories.attribute import AttributeDefinitionRepository
from app.repositories.entity import count_attribute_usage
from app.schemas.attribute import (
    ALLOWED_TYPES,
    AttributeDefinitionCreate,
    AttributeDefinitionUpdate,
)
from app.utils.errors import IntegrityViolation, NotFoundError, ValidationFailed

_ATTRIBUTE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class AttributeSchemaService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.attrs = AttributeDefinitionRepository(session)

    async def list_schema(self, entity_type: str) -> list[AttributeDefinition]:
        self._check_entity_type(entity_type)
        return await self.attrs.list_by_entity(entity_type)

    async def list_all(self) -> list[AttributeDefinition]:
        return await self.attrs.list_all()

    async def get(self, attr_id: uuid.UUID) -> AttributeDefinition:
        attr = await self.attrs.get_by_id(attr_id)
        if attr is None:
            raise NotFoundError("Атрибут не найден")
        return attr

    async def create(self, data: AttributeDefinitionCreate) -> AttributeDefinition:
        self._check_entity_type(data.entity_type)
        self._check_attribute_name(data.name)
        if data.data_type not in ALLOWED_TYPES:
            raise ValidationFailed(f"Неизвестный тип атрибута: {data.data_type}")
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
        if data.display_order is not None:
            attr.display_order = data.display_order
        if data.description is not None:
            attr.description = data.description
        if data.options is not None:
            attr.options = data.options
        await self.session.flush()
        return attr

    async def reorder(self, entity_type: str, order: list[uuid.UUID]) -> None:
        self._check_entity_type(entity_type)
        attrs = await self.attrs.list_by_entity(entity_type)
        existing_ids = {attr.id for attr in attrs}
        if len(order) != len(existing_ids) or set(order) != existing_ids:
            raise ValidationFailed(
                "Список атрибутов для сортировки не совпадает с атрибутами этого типа"
            )
        by_id = {attr.id: attr for attr in attrs}
        for idx, attr_id in enumerate(order):
            by_id[attr_id].display_order = (idx + 1) * 10
        await self.session.flush()

    async def delete(self, attr_id: uuid.UUID) -> None:
        attr = await self.attrs.get_by_id(attr_id)
        if attr is None:
            raise NotFoundError("Атрибут не найден")
        await self.attrs.delete(attr)
        await self.session.flush()

    async def usage(
        self, attrs: Sequence[AttributeDefinition]
    ) -> dict[uuid.UUID, dict[str, int]]:
        return await count_attribute_usage(self.session, attrs)

    @staticmethod
    def _check_entity_type(entity_type: str) -> None:
        if entity_type not in DATA_ENTITY_TYPES:
            raise ValidationFailed(f"Неизвестный тип сущности: {entity_type}")

    @staticmethod
    def _check_attribute_name(name: str) -> None:
        if not _ATTRIBUTE_NAME_RE.fullmatch(name):
            raise ValidationFailed(
                "Имя атрибута должно начинаться с латинской буквы и содержать "
                "только латинские буквы, цифры и подчёркивание; точки и другие "
                "разделители запрещены"
            )

    async def validate_attributes(
        self,
        entity_type: str,
        values: dict[str, Any],
        *,
        preserve_existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate ``values`` against active attribute_definitions and return normalized dict.

        - Required attributes must be present (non-empty).
        - Type-cast values when possible (date strings → date, numbers, bools, enum values).
        - Unknown attribute names are kept verbatim (so legacy values persist), but a warning
          could be added later if needed.
        - ``preserve_existing`` (the record's currently stored attributes) is used on updates
          to keep values whose attribute definition no longer exists at all — i.e. it was
          hard-deleted. Without this the next save would silently drop those values, even
          though the UI promises stored values stay untouched on deletion.
        """

        definitions = await self.attrs.list_by_entity(entity_type)
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

        # Preserve submitted values for keys outside the current schema (legacy keys, e.g.
        # from an import file):
        for k, v in values.items():
            if k not in defs_by_name and v not in (None, ""):
                normalized[k] = v

        # Preserve previously-stored values whose attribute definition no longer exists
        # (it was hard-deleted), so deleting an attribute doesn't silently drop data on the
        # next save. Keys that still have a definition are controlled by the form above, so
        # clearing them keeps working.
        if preserve_existing:
            for k, v in preserve_existing.items():
                if k not in defs_by_name and k not in normalized and v not in (None, ""):
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
        return value
