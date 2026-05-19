from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# Entity type identifiers used throughout the app.
ENTITY_CLIENT = "client"
ENTITY_ACCOUNT = "account"
ENTITY_CARD = "card"
ENTITY_TEMPLATE = "template"

REF_CURRENCY = "currency"
REF_ACCOUNT_TYPE = "account_type"
REF_CARD_TYPE = "card_type"
REF_BANK = "bank"
REF_CITIZENSHIP = "citizenship"

REFERENCE_TYPES: tuple[str, ...] = (
    REF_CURRENCY,
    REF_ACCOUNT_TYPE,
    REF_CARD_TYPE,
    REF_BANK,
    REF_CITIZENSHIP,
)

DATA_ENTITY_TYPES: tuple[str, ...] = (ENTITY_CLIENT, ENTITY_ACCOUNT, ENTITY_CARD)

ALL_ATTR_ENTITY_TYPES: tuple[str, ...] = DATA_ENTITY_TYPES + REFERENCE_TYPES


class AttributeDefinition(Base):
    __tablename__ = "attribute_definitions"
    __table_args__ = (
        UniqueConstraint("entity_type", "name", name="uq_attr_def_entity_name"),
        Index("ix_attr_def_entity", "entity_type"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    data_type: Mapped[str] = mapped_column(String(32), nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = _uuid_pk()
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    accounts: Mapped[list[Account]] = relationship(
        back_populates="client",
        passive_deletes=False,
        cascade="save-update, merge",
    )


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (Index("ix_accounts_client_id", "client_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="RESTRICT"),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    client: Mapped[Client] = relationship(back_populates="accounts")
    cards: Mapped[list[Card]] = relationship(
        back_populates="account",
        passive_deletes=False,
        cascade="save-update, merge",
    )


class Card(Base):
    __tablename__ = "cards"
    __table_args__ = (Index("ix_cards_account_id", "account_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    account: Mapped[Account] = relationship(back_populates="cards")


class ReferenceValue(Base):
    """Generic table for all reference data types (currency, account_type, ...).

    Using a single table keeps schema simple and lets us add new reference types
    via attribute_definitions only — no migration needed.
    """

    __tablename__ = "reference_values"
    __table_args__ = (
        UniqueConstraint("entity_type", "code", name="uq_ref_value_type_code"),
        Index("ix_ref_value_entity_type", "entity_type"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class MessageTemplate(Base):
    __tablename__ = "message_templates"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    format: Mapped[str] = mapped_column(String(16), nullable=False)  # 'json' | 'xml'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    original_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    llm_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    placeholders: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = _updated_at()
