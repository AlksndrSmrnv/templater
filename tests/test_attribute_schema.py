from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AttributeDefinition
from app.schemas.attribute import AttributeDefinitionUpdate
from app.services.attribute_schema import AttributeSchemaService
from app.utils.errors import ValidationFailed


@pytest.mark.parametrize(
    "name",
    [
        "passportNumber",
        "fullName",
        "citizenship_id",
    ],
)
def test_check_attribute_name_accepts_path_safe_names(name: str) -> None:
    AttributeSchemaService._check_attribute_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "passport.number",
        "passport number",
        "1passportNumber",
    ],
)
def test_check_attribute_name_rejects_path_unsafe_names(name: str) -> None:
    with pytest.raises(ValidationFailed, match="Имя атрибута"):
        AttributeSchemaService._check_attribute_name(name)


def _attr(name: str) -> AttributeDefinition:
    return AttributeDefinition(
        entity_type="client",
        name=name,
        label=name,
        data_type="string",
        is_required=False,
        options={},
    )


class _StubAttrRepo:
    """Minimal stand-in for AttributeDefinitionRepository (no DB)."""

    def __init__(self, defs: list[AttributeDefinition]):
        self._defs = defs

    async def list_by_entity(self, entity_type: str) -> list[AttributeDefinition]:
        return self._defs


def _service(defs: list[AttributeDefinition]) -> AttributeSchemaService:
    svc = AttributeSchemaService(cast(AsyncSession, None))
    svc.attrs = cast(Any, _StubAttrRepo(defs))
    return svc


async def test_update_preserves_value_of_hard_deleted_attribute() -> None:
    # "gone" has no definition (it was hard-deleted) — its stored value must survive a save.
    svc = _service([_attr("kept")])
    result = await svc.validate_attributes(
        "client",
        {"kept": "v1"},
        preserve_existing={"kept": "old", "gone": "orphan"},
    )
    assert result == {"kept": "v1", "gone": "orphan"}


async def test_update_clears_defined_attribute_when_form_omits_it() -> None:
    # "opt" still has a definition, so clearing it in the form must clear the stored value
    # (it must not be resurrected from preserve_existing).
    svc = _service([_attr("kept"), _attr("opt")])
    result = await svc.validate_attributes(
        "client",
        {"kept": "v1"},
        preserve_existing={"kept": "old", "opt": "oldOpt"},
    )
    assert result == {"kept": "v1"}


async def test_create_without_preserve_existing_keeps_legacy_keys_from_values() -> None:
    # Default behaviour is unchanged when no preserve_existing is passed (e.g. create/import).
    svc = _service([_attr("kept")])
    result = await svc.validate_attributes("client", {"kept": "v1", "legacy": "x"})
    assert result == {"kept": "v1", "legacy": "x"}


class _FakeSession:
    async def flush(self) -> None:
        return None


def _attr_with_id(name: str) -> AttributeDefinition:
    attr = _attr(name)
    attr.id = uuid.uuid4()
    return attr


async def test_reorder_assigns_sequential_display_order() -> None:
    a, b, c = _attr_with_id("a"), _attr_with_id("b"), _attr_with_id("c")
    svc = _service([a, b, c])
    svc.session = cast(AsyncSession, _FakeSession())
    # новый порядок: c, a, b → display_order 10, 20, 30
    await svc.reorder("client", [c.id, a.id, b.id])
    assert (c.display_order, a.display_order, b.display_order) == (10, 20, 30)


async def test_reorder_rejects_mismatched_id_set() -> None:
    a, b = _attr_with_id("a"), _attr_with_id("b")
    svc = _service([a, b])
    svc.session = cast(AsyncSession, _FakeSession())
    with pytest.raises(ValidationFailed, match="не совпадает"):
        await svc.reorder("client", [a.id, uuid.uuid4()])


async def test_reorder_rejects_duplicate_ids() -> None:
    a, b = _attr_with_id("a"), _attr_with_id("b")
    svc = _service([a, b])
    svc.session = cast(AsyncSession, _FakeSession())
    with pytest.raises(ValidationFailed, match="не совпадает"):
        await svc.reorder("client", [a.id, b.id, a.id])


class _GetByIdAttrRepo:
    """Stub AttributeDefinitionRepository exposing only get_by_id (for update)."""

    def __init__(self, attr: AttributeDefinition):
        self._attr = attr

    async def get_by_id(self, attr_id: uuid.UUID) -> AttributeDefinition:
        return self._attr


def _enum_attr() -> AttributeDefinition:
    attr = AttributeDefinition(
        entity_type="client",
        name="currency",
        label="Валюта",
        data_type="enum",
        is_required=False,
        options={"values": ["USD", "EUR"]},
    )
    attr.id = uuid.uuid4()
    return attr


def _update_service(attr: AttributeDefinition) -> AttributeSchemaService:
    svc = AttributeSchemaService(cast(AsyncSession, None))
    svc.attrs = cast(Any, _GetByIdAttrRepo(attr))
    svc.session = cast(AsyncSession, _FakeSession())
    return svc


async def test_update_enum_rejects_empty_values() -> None:
    # Clearing enum_values in the edit UI must NOT save {"values": []} — that would
    # leave an empty dropdown and break validate_attributes() for stored values.
    attr = _enum_attr()
    svc = _update_service(attr)
    with pytest.raises(ValidationFailed, match="enum"):
        await svc.update(attr.id, AttributeDefinitionUpdate(options={"values": []}))
    # The stored options must be left untouched on the rejected update.
    assert attr.options == {"values": ["USD", "EUR"]}


async def test_update_enum_accepts_new_values() -> None:
    attr = _enum_attr()
    svc = _update_service(attr)
    result = await svc.update(attr.id, AttributeDefinitionUpdate(options={"values": ["USD", "RUB"]}))
    assert result.options == {"values": ["USD", "RUB"]}
