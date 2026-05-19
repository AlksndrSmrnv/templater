from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class TemplateBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    format: Literal["json", "xml"] = "json"
    content: str


class TemplateCreate(TemplateBase):
    analyze_with_llm: bool = True


class TemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    content: str | None = None
    placeholders: list[dict[str, Any]] | None = None
    llm_meta: dict[str, Any] | None = None


class PlaceholderInfo(BaseModel):
    location: str  # JSONPath-like path or XPath
    mode: Literal["mapped", "literal"] = "literal"
    value: str  # current value (with {{...}} placeholder or original literal)
    original: str  # original raw value from uploaded template
    suggestion: str | None = None  # LLM-suggested placeholder path


class TemplateRead(TemplateBase):
    id: uuid.UUID
    original_content: str
    llm_meta: dict[str, Any]
    placeholders: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


class TemplateFillRequest(BaseModel):
    sender_client_id: uuid.UUID | None = None
    sender_account_id: uuid.UUID | None = None
    sender_card_id: uuid.UUID | None = None
    receiver_client_id: uuid.UUID | None = None
    receiver_account_id: uuid.UUID | None = None
    receiver_card_id: uuid.UUID | None = None


class TemplateFillResult(BaseModel):
    content: str
    format: Literal["json", "xml"]
    unresolved: list[str] = Field(default_factory=list)
