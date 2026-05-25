from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class ExportRequest(BaseModel):
    clients: list[uuid.UUID] = Field(default_factory=list)
    accounts: list[uuid.UUID] = Field(default_factory=list)
    cards: list[uuid.UUID] = Field(default_factory=list)
    templates: list[uuid.UUID] = Field(default_factory=list)


class ExportPackage(BaseModel):
    version: int = 1
    attribute_schema: list[dict[str, Any]]
    references: dict[str, list[dict[str, Any]]]
    clients: list[dict[str, Any]]
    accounts: list[dict[str, Any]]
    cards: list[dict[str, Any]]
    templates: list[dict[str, Any]]


class ImportSummary(BaseModel):
    created: dict[str, int]
    updated: dict[str, int]
    skipped: dict[str, int]
    errors: list[str] = Field(default_factory=list)
