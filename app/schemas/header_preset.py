from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class PresetHeaderIn(BaseModel):
    """One header row as submitted from the settings editor (key + value).

    ``mode``/``original``/``disabled`` are derived server-side by
    ``HeaderPresetService.normalize_headers`` — clients only send key/value.
    """

    key: str = Field(min_length=1, max_length=255)
    value: str = Field(default="", max_length=4096)


class HeaderPresetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    project_id: uuid.UUID
    url: str = Field(default="", max_length=4096)
    headers: list[PresetHeaderIn] = Field(default_factory=list)


class HeaderPresetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    project_id: uuid.UUID | None = None
    url: str | None = Field(default=None, max_length=4096)
    headers: list[PresetHeaderIn] | None = None
