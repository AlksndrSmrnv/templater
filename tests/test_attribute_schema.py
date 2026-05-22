from __future__ import annotations

import pytest

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
