from __future__ import annotations

from pydantic import BaseModel, Field

COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"


class AccessGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    color: str = Field(pattern=COLOR_PATTERN)
    password: str = Field(min_length=1, max_length=255)


class AccessGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    color: str | None = Field(default=None, pattern=COLOR_PATTERN)
    # Optional password change — a blank/omitted value leaves the password as is.
    password: str | None = Field(default=None, max_length=255)
