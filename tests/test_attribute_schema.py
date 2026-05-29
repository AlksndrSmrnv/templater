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
