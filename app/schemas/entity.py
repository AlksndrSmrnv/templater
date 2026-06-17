from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class ClientBase(BaseModel):
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    # Access group this client belongs to. ``None`` = public (visible to all);
    # a value must be a group the caller has unlocked (validated server-side).
    group_id: uuid.UUID | None = None


class ClientCreate(ClientBase):
    pass


class ClientUpdate(ClientBase):
    pass


class AccountBase(BaseModel):
    client_id: uuid.UUID
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class AccountCreate(AccountBase):
    pass


class AccountUpdate(AccountBase):
    pass


class CardBase(BaseModel):
    account_id: uuid.UUID
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class CardCreate(CardBase):
    pass


class CardUpdate(CardBase):
    pass
