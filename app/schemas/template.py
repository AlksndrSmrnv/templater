from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class TemplateBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    format: Literal["json", "xml"] = "json"
    content: str


class TemplateCreate(TemplateBase):
    analyze_with_llm: bool = True
    placeholders: list[dict[str, Any]] | None = None
    llm_meta: dict[str, Any] | None = None
    # Optional placement: drop the new request straight into a collection folder
    # (used by the "+ запрос" buttons in the collections tree).
    collection_id: uuid.UUID | None = None
    folder_path: list[str] = Field(default_factory=list)


class TemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    content: str | None = None
    placeholders: list[dict[str, Any]] | None = None
    llm_meta: dict[str, Any] | None = None
    headers: list[dict[str, Any]] | None = None
    http_method: str | None = None
    url: str | None = None


class PlaceholderInfo(BaseModel):
    location: str = Field(min_length=1)  # JSONPath-like path or XPath
    mode: Literal["mapped", "literal", "dynamic"] = "literal"
    value: str  # current value (with {{...}} placeholder or original literal)
    original: str = ""  # original raw value from uploaded template
    suggestion: str | None = None  # LLM-suggested placeholder path


class TemplateFillRequest(BaseModel):
    sender_client_id: uuid.UUID | None = None
    sender_account_id: uuid.UUID | None = None
    sender_card_id: uuid.UUID | None = None
    receiver_client_id: uuid.UUID | None = None
    receiver_account_id: uuid.UUID | None = None
    receiver_card_id: uuid.UUID | None = None
    account_owner_client_id: uuid.UUID | None = None
    account_owner_account_id: uuid.UUID | None = None
    account_owner_card_id: uuid.UUID | None = None
