from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AttributeDefinition
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


def _attr(name: str, *, is_deprecated: bool = False) -> AttributeDefinition:
    return AttributeDefinition(
        entity_type="client",
        name=name,
        label=name,
        data_type="string",
        is_required=False,
        is_deprecated=is_deprecated,
        options={},
    )


class _StubAttrRepo:
    """Minimal stand-in for AttributeDefinitionRepository (no DB)."""

    def __init__(self, active: list[AttributeDefinition], deprecated: list[AttributeDefinition]):
        self._active = active
        self._all = active + deprecated

    async def list_by_entity(
        self, entity_type: str, *, include_deprecated: bool = True
    ) -> list[AttributeDefinition]:
        return self._all if include_deprecated else self._active


def _service(active: list[AttributeDefinition], deprecated: list[AttributeDefinition]) -> AttributeSchemaService:
    svc = AttributeSchemaService(cast(AsyncSession, None))
    svc.attrs = cast(Any, _StubAttrRepo(active, deprecated))
    return svc


async def test_update_preserves_value_of_hard_deleted_attribute() -> None:
    # "gone" has no definition at all (it was hard-deleted) — its stored value must survive.
    svc = _service(active=[_attr("kept")], deprecated=[])
    result = await svc.validate_attributes(
        "client",
        {"kept": "v1"},
        preserve_existing={"kept": "old", "gone": "orphan"},
    )
    assert result == {"kept": "v1", "gone": "orphan"}


async def test_update_does_not_resurrect_cleared_deprecated_attribute() -> None:
    # "dep" still has a (deprecated) definition, so clearing it in the form must clear it.
    svc = _service(active=[_attr("kept")], deprecated=[_attr("dep", is_deprecated=True)])
    result = await svc.validate_attributes(
        "client",
        {"kept": "v1"},
        preserve_existing={"kept": "old", "dep": "oldDep"},
    )
    assert result == {"kept": "v1"}


async def test_update_keeps_resubmitted_deprecated_value() -> None:
    svc = _service(active=[_attr("kept")], deprecated=[_attr("dep", is_deprecated=True)])
    result = await svc.validate_attributes(
        "client",
        {"kept": "v1", "dep": "stillHere"},
        preserve_existing={"dep": "old"},
    )
    assert result == {"kept": "v1", "dep": "stillHere"}


async def test_create_without_preserve_existing_keeps_legacy_keys_from_values() -> None:
    # Default behaviour is unchanged when no preserve_existing is passed (e.g. create/import).
    svc = _service(active=[_attr("kept")], deprecated=[])
    result = await svc.validate_attributes("client", {"kept": "v1", "legacy": "x"})
    assert result == {"kept": "v1", "legacy": "x"}
