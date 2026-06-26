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
    text,
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
    __table_args__ = (Index("ix_clients_group_id", "group_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Access-group membership. ``NULL`` = public (visible to everyone); a set
    # value hides the client (and its accounts/cards) behind that group's
    # password. RESTRICT is a backstop — AccessGroupService refuses to delete a
    # group while any client references it.
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("access_groups.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # ``selectin`` so the group badge renders from Jinja without an explicit
    # eager-load (async lazy access would raise MissingGreenlet) — same pattern
    # as ``MessageTemplate.project``.
    group: Mapped[AccessGroup | None] = relationship(lazy="selectin")
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


class AccessGroup(Base):
    """A password-protected vault for sensitive test data.

    Plain message templates stay public to everyone; only the *filled-in* data
    is sensitive. A user "unlocks" a group by entering its password — the server
    then issues an HMAC-signed cookie carrying the set of unlocked group ids (see
    ``app/utils/access_groups.py``). Clients (and, transitively, their accounts
    and cards) and filled templates can be tagged with a group; a ``NULL`` tag
    means public/visible to everyone.

    There are no user accounts: knowing a group's password *is* membership. The
    password is stored only as a salted PBKDF2 hash (``app/utils/password.py``).
    Carries a highlight ``color`` like :class:`Project` so the UI renders an
    explicit badge. Deletion is refused at the service level while any client or
    filled template references the group (the RESTRICT FKs are a backstop), so a
    group can never be removed in a way that silently exposes private data.
    """

    __tablename__ = "access_groups"
    __table_args__ = (UniqueConstraint("name", name="uq_access_groups_name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="#9E9E9E")
    # Salted PBKDF2 hash, format ``pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>``.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class HeaderPreset(Base):
    """A reusable endpoint preset: a standard URL + set of HTTP headers.

    Tagged with exactly one :class:`Project` (the same label used on templates)
    so the template UI can offer only the presets matching a template's project.
    Applying a preset *copies* its ``url`` and ``headers`` onto the template —
    there is no live link, so a preset can be edited or deleted freely without
    touching templates that already used it. Each ``headers`` entry follows the
    shared header shape ``{"key","value","mode","original","disabled"}``; a value
    containing ``{{…}}`` (e.g. ``{{rquid}}``) is stored with ``mode="dynamic"``
    and resolved at send time (not yet implemented).
    """

    __tablename__ = "header_presets"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_header_presets_project_name"),
        Index("ix_header_presets_project_id", "project_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # HTTP headers: list of {"key","value","mode","original","disabled"}.
    headers: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    # Lightweight config keyed to a project — CASCADE so deleting a project (only
    # possible once no templates reference it) cleans up its presets too.
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``selectin`` so the project badge renders in Jinja without an explicit
    # eager-load (async lazy access would raise MissingGreenlet).
    project: Mapped[Project] = relationship(lazy="selectin")
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


class CollectionJob(Base):
    """Background LLM-processing job for a :class:`Collection`.

    One collection has at most one *active* job (``status`` in
    ``pending|running``) at a time. This is enforced two ways:
    :meth:`CollectionJobService.start` checks ``find_active`` as a fast path
    (clean error without a constraint violation in the common case), and the
    partial unique index ``uq_collection_jobs_one_active`` is the race-condition
    backstop — two strictly concurrent POSTs that both pass the check can't both
    insert; the loser gets an ``IntegrityError`` that ``start`` turns into the
    same user-facing error. A job is created ``pending`` with ``total=0`` and
    flipped to ``running`` once the background coroutine starts; counts
    (``processed``/``skipped``/``failed``) are incremented atomically as each
    template resolves, so the polling endpoint never observes torn state.
    ``done`` (all good or per-template failures absorbed) and ``failed`` (the
    orchestrator itself blew up) are terminal. On server restart ``reconcile``
    rewrites any still-pending/running rows to ``failed`` — the in-process task
    is gone.
    """

    __tablename__ = "collection_jobs"
    __table_args__ = (
        Index("ix_collection_jobs_collection_status", "collection_id", "status"),
        # At most one pending/running job per collection — the DB-level backstop
        # for the "one active job" rule. Mirrors the partial unique index created
        # in migration 0014 so ``alembic revision --autogenerate`` doesn't emit a
        # spurious DROP for it.
        Index(
            "uq_collection_jobs_one_active",
            "collection_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Job-level failure text (set only when the orchestrator itself blows up,
    # not for per-template LLM failures — those bump ``failed``).
    error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
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
        Index("ix_filled_templates_group_id", "group_id"),
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

    # Access-group membership, derived from the sender client at fill time.
    # ``NULL`` = public. RESTRICT is a backstop (deletion guarded in the
    # service). Name/color are snapshotted — like the project badge above — so
    # the group badge survives the group being deleted.
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("access_groups.id", ondelete="RESTRICT"),
        nullable=True,
    )
    group_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    group_color_snapshot: Mapped[str] = mapped_column(String(16), nullable=False, default="")

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

    # Materialised folder path on the «Заполненные шаблоны» page, e.g.
    # ["Проект", "Релиз", "Фича"]. Explicit (possibly empty) folders live in
    # the ``filled_root_folders`` app setting — same scheme as message
    # templates use with collections/root folders.
    folder_path: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # HTTP request snapshot copied from the source template at save time, so
    # the filled template stays runnable (future "send request" feature) even
    # after the source template changes or is deleted.
    http_method_snapshot: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    url_snapshot: Mapped[str] = mapped_column(Text, nullable=False, default="")
    headers_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class RequestChain(Base):
    """An ordered chain of REST requests built from filled templates.

    A chain lives in the same folder tree as filled templates (it carries a
    materialised ``folder_path`` and a ``display_order``) and is shown in the
    «Заполненные шаблоны» workspace as a distinct node type. Each step
    (:class:`RequestChainStep`) snapshots one filled template's request so the
    chain stays runnable after the source is edited or deleted. Later steps may
    reference fields of earlier steps' responses via ``{{ $N.path }}`` tokens
    stored inline in the step body — there is no real sending yet, a stub seam
    echoes an editable example response.
    """

    __tablename__ = "request_chains"
    __table_args__ = (
        Index("ix_request_chains_group_id", "group_id"),
        Index("ix_request_chains_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Materialised folder path on the «Заполненные шаблоны» page, shared with
    # filled templates (explicit folders live in the ``filled_root_folders`` app
    # setting). ``display_order`` orders chains among their folder siblings.
    folder_path: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Access-group visibility, mirroring ``FilledTemplate.group_id``: ``NULL`` =
    # public. A chain may hold public steps freely; the first step from a private
    # group sets this, and a step from a conflicting group is rejected (so a
    # single ``group_id`` never has to hide one group's data from another).
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("access_groups.id", ondelete="RESTRICT"),
        nullable=True,
    )
    group_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    group_color_snapshot: Mapped[str] = mapped_column(String(16), nullable=False, default="")

    steps: Mapped[list[RequestChainStep]] = relationship(
        back_populates="chain",
        order_by="RequestChainStep.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class RequestChainStep(Base):
    """One request in a :class:`RequestChain`, snapshotted from a filled template.

    The request envelope (method/url/headers/body/format) is copied at add time
    so the step survives the source filled template being edited or deleted.
    ``body`` may contain ``{{ $N.path }}`` reference tokens; ``mock_response`` is
    the editable JSON example the stub send echoes back.
    """

    __tablename__ = "request_chain_steps"
    __table_args__ = (
        Index("ix_request_chain_steps_chain_id", "chain_id"),
        # ``position`` drives the {{ $N.path }} reference numbering, so a
        # duplicate would silently corrupt which step a reference points at.
        # DEFERRABLE INITIALLY DEFERRED lets reorder/remove renumber the whole
        # chain to 0..n-1 within one transaction (positions may collide
        # mid-flush); the check runs at COMMIT, when they are unique again. A
        # concurrent add racing on max(position)+1 then surfaces as a clean 409.
        UniqueConstraint(
            "chain_id",
            "position",
            name="uq_request_chain_steps_chain_position",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    chain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("request_chains.id", ondelete="CASCADE"),
        nullable=False,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Audit link to the source filled template (SET NULL so deleting it never
    # blocks); the snapshot columns keep the step runnable regardless.
    filled_template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("filled_templates.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Role bindings copied from the source filled template at add time. They let
    # the «Заменить клиента» menu re-point a role and re-render this step's body
    # from the source message template (reached via ``filled_template_id`` →
    # ``message_template_id``). All ``ON DELETE SET NULL`` like FilledTemplate.
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
    # {"sender": "Иванов · ACC-001", ...} — same shape as FilledTemplate's.
    role_labels_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    format: Mapped[str] = mapped_column(String(16), nullable=False, default="json")
    http_method_snapshot: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    url_snapshot: Mapped[str] = mapped_column(Text, nullable=False, default="")
    headers_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    mock_response: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # JSON-pointer / XML-path locations whose leaf was filled with concrete test
    # data at fill time (snapshotted from the source filled template). Drives the
    # green «заполнено тестовыми данными» colour in the chain UI; the remaining
    # marks (blue dynamic ``{{token}}``, purple ``{{ $N.path }}`` references,
    # white literals) are derivable from the body text itself.
    changed_locations: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # Reset-buffer for click-to-bind: maps a leaf location to its original
    # (typed, so a JSON number resets to a number) value before it was bound to
    # a previous step's response field. The active reference lives inline in
    # ``body`` (``{{ $N.path }}``); this only lets the UI restore the pre-bind
    # value on «Сбросить».
    bindings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    chain: Mapped[RequestChain] = relationship(back_populates="steps")

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
