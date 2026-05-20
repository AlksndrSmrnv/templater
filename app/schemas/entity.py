from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ClientBase(BaseModel):
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class ClientCreate(ClientBase):
    pass


class ClientUpdate(ClientBase):
    pass


class ClientRead(ClientBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class AccountBase(BaseModel):
    client_id: uuid.UUID
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class AccountCreate(AccountBase):
    pass


class AccountUpdate(AccountBase):
    pass


class AccountRead(AccountBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class CardBase(BaseModel):
    account_id: uuid.UUID
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class CardCreate(CardBase):
    pass


class CardUpdate(CardBase):
    pass


class CardRead(CardBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
