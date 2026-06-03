from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

ALLOWED_TYPES = {"string", "int", "number", "bool", "date", "datetime", "text", "enum"}


class AttributeDefinitionBase(BaseModel):
    entity_type: str
    name: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=255)
    data_type: str
    is_required: bool = False
    display_order: int = 0
    description: str = ""
    options: dict[str, Any] = Field(default_factory=dict)


class AttributeDefinitionCreate(AttributeDefinitionBase):
    pass


class AttributeDefinitionUpdate(BaseModel):
    label: str | None = None
    is_required: bool | None = None
    display_order: int | None = None
    description: str | None = None
    options: dict[str, Any] | None = None


class AttributeReorder(BaseModel):
    entity_type: str
    order: list[uuid.UUID]
