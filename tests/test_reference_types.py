from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.reference_types import ReferenceTypeService
from app.utils.errors import ValidationFailed


def _service() -> ReferenceTypeService:
    # The length/format/reserved checks all run before any repository call, so a
    # null session is enough to exercise them.
    return ReferenceTypeService(cast(AsyncSession, None))


@pytest.mark.asyncio
async def test_create_rejects_invalid_code() -> None:
    with pytest.raises(ValidationFailed, match="Код справочника"):
        await _service().create(code="Bad Code", title="Отделы", icon="", description="", columns=[])


@pytest.mark.asyncio
async def test_create_rejects_too_long_code() -> None:
    with pytest.raises(ValidationFailed, match="64"):
        await _service().create(code="a" * 65, title="X", icon="", description="", columns=[])


@pytest.mark.asyncio
async def test_create_rejects_reserved_code() -> None:
    with pytest.raises(ValidationFailed, match="зарезервирован"):
        await _service().create(code="client", title="X", icon="", description="", columns=[])


@pytest.mark.asyncio
async def test_create_rejects_empty_title() -> None:
    with pytest.raises(ValidationFailed, match="обязательно"):
        await _service().create(code="dept", title="   ", icon="", description="", columns=[])


@pytest.mark.asyncio
async def test_create_rejects_too_long_title() -> None:
    with pytest.raises(ValidationFailed, match="255"):
        await _service().create(code="dept", title="x" * 256, icon="", description="", columns=[])


@pytest.mark.asyncio
async def test_create_rejects_too_long_icon() -> None:
    with pytest.raises(ValidationFailed, match="Иконка"):
        await _service().create(code="dept", title="Отделы", icon="x" * 17, description="", columns=[])
