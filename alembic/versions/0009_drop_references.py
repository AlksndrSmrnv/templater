"""Drop the reference (справочники) machinery entirely

The reference-types/reference-values feature is removed in favour of the
``enum`` attribute type. This migration performs a one-time, intentionally
destructive cleanup mirroring the style of ``0006_reference_types_registry``:

  1. the JSONB keys that ref-typed attributes wrote into clients/accounts/cards
     are stripped (the attribute name → JSONB key, its ``entity_type`` → table);
  2. the ``data_type = 'ref'`` attribute definitions are deleted;
  3. the attribute definitions that *described columns of* a reference type are
     deleted (their ``entity_type`` is the reference-type code, i.e. anything
     that isn't a core data entity);
  4. the ``reference_values`` and ``reference_types`` tables are dropped.

Revision ID: 0009_drop_references
Revises: 0008_collection_folders
Create Date: 2026-06-03

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0009_drop_references"
down_revision = "0008_collection_folders"
branch_labels = None
depends_on = None

# Core data entities — everything else in attribute_definitions.entity_type was a
# reference-type code and is removed along with its rows.
_DATA_ENTITY_TYPES = ("client", "account", "card")
_ENTITY_TABLES = {"client": "clients", "account": "accounts", "card": "cards"}


def upgrade() -> None:
    bind = op.get_bind()

    # 1) Strip orphaned JSONB keys for every ref-typed attribute, keyed by the
    #    owning entity's table. Collected before we delete the definitions.
    ref_attrs = bind.execute(
        sa.text(
            "SELECT entity_type, name FROM attribute_definitions WHERE data_type = 'ref'"
        )
    ).all()
    for entity_type, name in ref_attrs:
        table = _ENTITY_TABLES.get(entity_type)
        if table is None:
            continue
        bind.execute(
            sa.text(f"UPDATE {table} SET attributes = attributes - :key"), {"key": name}
        )

    # 2) ref-typed attribute definitions on the data entities.
    bind.execute(sa.text("DELETE FROM attribute_definitions WHERE data_type = 'ref'"))

    # 3) Attribute definitions that described columns of a reference type — their
    #    entity_type is the reference-type code, never a core data entity.
    bind.execute(
        sa.text(
            "DELETE FROM attribute_definitions "
            "WHERE entity_type NOT IN :data_types"
        ).bindparams(sa.bindparam("data_types", _DATA_ENTITY_TYPES, expanding=True))
    )

    # 4) Drop the reference tables.
    op.drop_index("ix_ref_value_entity_type", table_name="reference_values")
    op.drop_table("reference_values")
    op.drop_table("reference_types")


def downgrade() -> None:
    # One-way by design: the tables are recreated empty so the schema can be
    # rolled back, but the deleted reference data and stripped JSONB keys are
    # NOT restored — that information is gone.
    op.create_table(
        "reference_types",
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("icon", sa.String(16), nullable=False, server_default=sa.text("''")),
        sa.Column("description", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("display_order", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "reference_values",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("code", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("attributes", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("entity_type", "code", name="uq_ref_value_type_code"),
    )
    op.create_index("ix_ref_value_entity_type", "reference_values", ["entity_type"])
