"""Reference-type registry + removal of account_type/card_type/bank/citizenship

Introduces the ``reference_types`` registry table (which makes the set of
reference tables data instead of hardcoded constants) and seeds the surviving
``currency`` type.

Also performs a one-time, intentionally destructive cleanup: the reference
tables ``account_type`` (Типы счетов), ``card_type`` (Типы карт), ``bank``
(Банки) and ``citizenship`` (Гражданство) are removed "as if they never
existed" — their values, their own column definitions, the entity attributes
that pointed at them, and the corresponding keys inside the JSONB ``attributes``
of clients/accounts/cards are all deleted.

Revision ID: 0006_reference_types_registry
Revises: 0005_collections
Create Date: 2026-06-01

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006_reference_types_registry"
down_revision = "0005_collections"
branch_labels = None
depends_on = None


# Reference types being removed completely.
REMOVED_TYPES = ("account_type", "card_type", "bank", "citizenship")


def upgrade() -> None:
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

    # Seed only the surviving reference type. account_type/card_type/bank/
    # citizenship are deliberately NOT registered — they are being removed below.
    reference_types = sa.table(
        "reference_types",
        sa.column("code", sa.String),
        sa.column("title", sa.String),
        sa.column("icon", sa.String),
        sa.column("description", sa.Text),
        sa.column("display_order", sa.Integer),
    )
    op.bulk_insert(
        reference_types,
        [{"code": "currency", "title": "Валюты", "icon": "💱", "description": "", "display_order": 10}],
    )

    bind = op.get_bind()
    removed = list(REMOVED_TYPES)

    # 1) Reference values of the removed types.
    bind.execute(
        sa.text("DELETE FROM reference_values WHERE entity_type = ANY(:types)"),
        {"types": removed},
    )

    # 2) The removed types' own column definitions (e.g. bank.bic, citizenship.iso2).
    bind.execute(
        sa.text("DELETE FROM attribute_definitions WHERE entity_type = ANY(:types)"),
        {"types": removed},
    )

    # 3) Entity attributes that referenced the removed types
    #    (account.account_type_id, account.bank_id, card.card_type_id, client.citizenship_id).
    bind.execute(
        sa.text(
            "DELETE FROM attribute_definitions "
            "WHERE data_type = 'ref' AND options->>'ref_entity' = ANY(:types)"
        ),
        {"types": removed},
    )

    # 4) Strip the now-orphaned keys from entity JSONB attributes.
    bind.execute(sa.text("UPDATE accounts SET attributes = attributes - 'account_type_id' - 'bank_id'"))
    bind.execute(sa.text("UPDATE cards SET attributes = attributes - 'card_type_id'"))
    bind.execute(sa.text("UPDATE clients SET attributes = attributes - 'citizenship_id'"))


def downgrade() -> None:
    # Intentionally one-way: the registry table is dropped, but the deleted
    # reference data (values, attribute definitions, stripped JSONB keys) is NOT
    # restored — that information is gone by design.
    op.drop_table("reference_types")
