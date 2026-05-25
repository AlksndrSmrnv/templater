from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ReferenceValueBase(BaseModel):
    entity_type: str
    code: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)


class ReferenceValueCreate(ReferenceValueBase):
    pass


class ReferenceValueUpdate(BaseModel):
    code: str | None = None
    name: str | None = None
    description: str | None = None
    attributes: dict[str, Any] | None = None
