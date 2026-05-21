"""Initial schema + default attribute definitions

Single migration for a clean install: creates all tables and seeds the
default ``attribute_definitions`` (the app's "schema as data"). Reference
*values* (currencies, banks, ...) are seeded separately and idempotently by
``scripts/seed_reference_data.py``.

Revision ID: 0001_init
Revises:
Create Date: 2026-05-20

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


# Default attribute schema for the core entities and reference types.
SEED: list[dict[str, object]] = [
    # Client
    {"entity_type": "client", "name": "fullName", "label": "ФИО", "data_type": "string", "is_required": True, "display_order": 10, "description": "Полное имя клиента"},
    {"entity_type": "client", "name": "birthDate", "label": "Дата рождения", "data_type": "date", "is_required": False, "display_order": 20, "description": "Дата рождения клиента"},
    {"entity_type": "client", "name": "citizenship_id", "label": "Гражданство", "data_type": "ref", "is_required": False, "display_order": 30, "options": {"ref_entity": "citizenship"}, "description": "Гражданство клиента"},
    {"entity_type": "client", "name": "passport.series", "label": "Серия паспорта", "data_type": "string", "is_required": False, "display_order": 40},
    {"entity_type": "client", "name": "passport.number", "label": "Номер паспорта", "data_type": "string", "is_required": False, "display_order": 50},
    {"entity_type": "client", "name": "inn", "label": "ИНН", "data_type": "string", "is_required": False, "display_order": 60},
    {"entity_type": "client", "name": "phone", "label": "Телефон", "data_type": "string", "is_required": False, "display_order": 70},
    {"entity_type": "client", "name": "email", "label": "Email", "data_type": "string", "is_required": False, "display_order": 80},
    {"entity_type": "client", "name": "residency", "label": "Резидентство", "data_type": "enum", "is_required": False, "display_order": 90, "options": {"values": ["resident", "non_resident"]}},
    {"entity_type": "client", "name": "capacity", "label": "Дееспособность", "data_type": "enum", "is_required": False, "display_order": 100, "options": {"values": ["capable", "limited", "incapable"]}},

    # Account
    {"entity_type": "account", "name": "number", "label": "Номер счёта", "data_type": "string", "is_required": True, "display_order": 10},
    {"entity_type": "account", "name": "currency_id", "label": "Валюта", "data_type": "ref", "is_required": True, "display_order": 20, "options": {"ref_entity": "currency"}},
    {"entity_type": "account", "name": "account_type_id", "label": "Тип счёта", "data_type": "ref", "is_required": False, "display_order": 30, "options": {"ref_entity": "account_type"}},
    {"entity_type": "account", "name": "bank_id", "label": "Банк", "data_type": "ref", "is_required": False, "display_order": 40, "options": {"ref_entity": "bank"}},
    {"entity_type": "account", "name": "balance", "label": "Баланс", "data_type": "number", "is_required": False, "display_order": 50},
    {"entity_type": "account", "name": "openedAt", "label": "Дата открытия", "data_type": "date", "is_required": False, "display_order": 60},

    # Card
    {"entity_type": "card", "name": "number", "label": "Номер карты", "data_type": "string", "is_required": True, "display_order": 10},
    {"entity_type": "card", "name": "holderName", "label": "Имя держателя", "data_type": "string", "is_required": False, "display_order": 20},
    {"entity_type": "card", "name": "card_type_id", "label": "Тип карты", "data_type": "ref", "is_required": False, "display_order": 30, "options": {"ref_entity": "card_type"}},
    {"entity_type": "card", "name": "expiry", "label": "Срок действия", "data_type": "string", "is_required": False, "display_order": 40},
    {"entity_type": "card", "name": "cvv", "label": "CVV", "data_type": "string", "is_required": False, "display_order": 50},

    # Currency (reference)
    {"entity_type": "currency", "name": "iso_code", "label": "ISO код", "data_type": "string", "is_required": False, "display_order": 10},
    {"entity_type": "currency", "name": "numeric_code", "label": "Цифровой код", "data_type": "string", "is_required": False, "display_order": 20},
    {"entity_type": "currency", "name": "symbol", "label": "Символ", "data_type": "string", "is_required": False, "display_order": 30},

    # Bank (reference)
    {"entity_type": "bank", "name": "bic", "label": "БИК", "data_type": "string", "is_required": False, "display_order": 10},
    {"entity_type": "bank", "name": "swift", "label": "SWIFT", "data_type": "string", "is_required": False, "display_order": 20},
    {"entity_type": "bank", "name": "country", "label": "Страна", "data_type": "string", "is_required": False, "display_order": 30},

    # Citizenship (reference)
    {"entity_type": "citizenship", "name": "iso2", "label": "ISO-2", "data_type": "string", "is_required": False, "display_order": 10},
    {"entity_type": "citizenship", "name": "iso3", "label": "ISO-3", "data_type": "string", "is_required": False, "display_order": 20},

    # account_type, card_type — only the built-in code/name/description columns.
]


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    op.create_table(
        "attribute_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("data_type", sa.String(32), nullable=False),
        sa.Column("is_required", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("is_deprecated", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("display_order", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("description", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("options", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("entity_type", "name", name="uq_attr_def_entity_name"),
    )
    op.create_index("ix_attr_def_entity", "attribute_definitions", ["entity_type"])

    op.create_table(
        "clients",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("description", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("tags", postgresql.ARRAY(sa.String), nullable=False, server_default=sa.text("'{}'::varchar[]")),
        sa.Column("attributes", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("description", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("tags", postgresql.ARRAY(sa.String), nullable=False, server_default=sa.text("'{}'::varchar[]")),
        sa.Column("attributes", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_accounts_client_id", "accounts", ["client_id"])

    op.create_table(
        "cards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("description", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("tags", postgresql.ARRAY(sa.String), nullable=False, server_default=sa.text("'{}'::varchar[]")),
        sa.Column("attributes", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_cards_account_id", "cards", ["account_id"])

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

    op.create_table(
        "message_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("original_content", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("llm_meta", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("placeholders", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", postgresql.JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # Seed the default attribute schema. ``options`` is inserted as a real dict
    # against a JSONB column, so it lands as a JSON object (not a quoted string).
    attribute_definitions = sa.table(
        "attribute_definitions",
        sa.column("entity_type", sa.String),
        sa.column("name", sa.String),
        sa.column("label", sa.String),
        sa.column("data_type", sa.String),
        sa.column("is_required", sa.Boolean),
        sa.column("is_deprecated", sa.Boolean),
        sa.column("display_order", sa.Integer),
        sa.column("description", sa.Text),
        sa.column("options", postgresql.JSONB),
    )
    op.bulk_insert(
        attribute_definitions,
        [
            {
                "entity_type": item["entity_type"],
                "name": item["name"],
                "label": item["label"],
                "data_type": item["data_type"],
                "is_required": item.get("is_required", False),
                "is_deprecated": False,
                "display_order": item.get("display_order", 0),
                "description": item.get("description", ""),
                "options": item.get("options", {}),
            }
            for item in SEED
        ],
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_table("message_templates")
    op.drop_index("ix_ref_value_entity_type", table_name="reference_values")
    op.drop_table("reference_values")
    op.drop_index("ix_cards_account_id", table_name="cards")
    op.drop_table("cards")
    op.drop_index("ix_accounts_client_id", table_name="accounts")
    op.drop_table("accounts")
    op.drop_table("clients")
    op.drop_index("ix_attr_def_entity", table_name="attribute_definitions")
    op.drop_table("attribute_definitions")
