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

# The entity types that own user-defined attribute definitions. Each maps to a
# concrete data table (clients/accounts/cards); ``template`` is reserved but not
# an attribute owner.
DATA_ENTITY_TYPES: tuple[str, ...] = (ENTITY_CLIENT, ENTITY_ACCOUNT, ENTITY_CARD)

# Entity-type codes that name core data entities / templates and are reserved.
RESERVED_ENTITY_TYPES: frozenset[str] = frozenset(
    {ENTITY_CLIENT, ENTITY_ACCOUNT, ENTITY_CARD, ENTITY_TEMPLATE}
)


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


class Project(Base):
    """A user-defined project used to tag templates.

    Every :class:`MessageTemplate` belongs to exactly one project; the project
    carries a highlight color so the UI can render an explicit badge on each
    template. Deletion is refused at the service level while templates
    reference the project (the FK is RESTRICT as a backstop).
    """

    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("name", name="uq_projects_name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="#9E9E9E")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Collection(Base):
    """An imported request collection (Postman v2.1, later Insomnia, …).

    Each contained request becomes a :class:`MessageTemplate` linked back via
    ``MessageTemplate.collection_id``. Folders are not modelled as rows — each
    template carries its ``folder_path`` (materialised path) and the UI rebuilds
    the tree from those paths.
    """

    __tablename__ = "collections"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="postman")
    source_format: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    variables: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    # Explicit folder paths (each path = list of segments, e.g. ["Transfers", "A2A"]).
    # Templates carry their own ``folder_path`` for membership, but that alone can't
    # represent an *empty* folder — without this the folder would vanish on the next
    # tree rebuild. So created/renamed folders are persisted here, giving folder
    # rename/delete a home and letting the workspace tree show empty folders.
    folders: Mapped[list[list[str]]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class MessageTemplate(Base):
    __tablename__ = "message_templates"
    __table_args__ = (
        Index("ix_message_templates_collection_id", "collection_id"),
        Index("ix_message_templates_project_id", "project_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    format: Mapped[str] = mapped_column(String(16), nullable=False)  # 'json' | 'xml'
    content: Mapped[str] = mapped_column(Text, nullable=False)
    original_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    llm_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    placeholders: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    # Last LLM request/response (system + user prompt and raw response) captured
    # during analysis. Persisted so the debug panel can show it after the fact —
    # e.g. when the template was processed in bulk from the collections menu.
    llm_debug: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, default=None)

    # Collection membership. ``collection_id`` is SET NULL on collection delete
    # so a template can outlive its collection (becomes "ungrouped"); the import
    # flow normally deletes a collection's templates explicitly.
    collection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("collections.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Every template belongs to exactly one project (RESTRICT is a backstop —
    # ProjectService refuses deletion while templates reference the project).
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # ``selectin`` so the badge is renderable from Jinja without an explicit
    # eager-load on every query (async lazy access would raise MissingGreenlet).
    project: Mapped[Project] = relationship(lazy="selectin")

    # Materialised folder path within the collection, e.g. ["Transfers", "A2A"].
    folder_path: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # HTTP request headers: list of {"key","value","mode","original","disabled"}.
    headers: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    http_method: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = _updated_at()


class FilledTemplate(Base):
    """Snapshot of a rendered template filled with concrete client data.

    Stores both the rendered ``filled_content`` (for fidelity after upstream
    deletes) and audit FKs to the source template/clients/accounts/cards.
    All FKs use ``ON DELETE SET NULL`` so deleting an upstream entity never
    blocks; the UI falls back to ``*_snapshot`` columns to keep the row
    readable.
    """

    __tablename__ = "filled_templates"
    __table_args__ = (
        Index("ix_filled_templates_template_id", "message_template_id"),
        Index("ix_filled_templates_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    format: Mapped[str] = mapped_column(String(16), nullable=False)  # 'json' | 'xml'
    filled_content: Mapped[str] = mapped_column(Text, nullable=False)
    changed_locations: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    unresolved: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    message_template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("message_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    template_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Project of the source template at fill time. Snapshotted (like the name
    # above) so the badge survives template deletion, after which the project
    # is unreachable via the relationship.
    project_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    project_color_snapshot: Mapped[str] = mapped_column(String(16), nullable=False, default="")

    sender_client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True
    )
    sender_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    sender_card_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cards.id", ondelete="SET NULL"), nullable=True
    )
    receiver_client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True
    )
    receiver_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    receiver_card_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cards.id", ondelete="SET NULL"), nullable=True
    )
    account_owner_client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True
    )
    account_owner_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    account_owner_card_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cards.id", ondelete="SET NULL"), nullable=True
    )

    # {"sender": "Иванов · ACC-001", "receiver": "...", "accountOwner": "..."}
    role_labels_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
