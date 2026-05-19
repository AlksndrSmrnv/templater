"""Seed default attribute_definitions for core entities and reference types

Revision ID: 0002_seed_attribute_schema
Revises: 0001_init_core
Create Date: 2026-05-19

"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0002_seed_attribute_schema"
down_revision = "0001_init_core"
branch_labels = None
depends_on = None


SEED: list[dict] = [
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

    # account_type, card_type — только базовые поля (code/name/description уже есть в reference_values)
]


def upgrade() -> None:
    table = sa.table(
        "attribute_definitions",
        sa.column("entity_type", sa.String),
        sa.column("name", sa.String),
        sa.column("label", sa.String),
        sa.column("data_type", sa.String),
        sa.column("is_required", sa.Boolean),
        sa.column("is_deprecated", sa.Boolean),
        sa.column("display_order", sa.Integer),
        sa.column("description", sa.Text),
        sa.column("options", sa.JSON),
    )
    rows = []
    for item in SEED:
        rows.append(
            {
                "entity_type": item["entity_type"],
                "name": item["name"],
                "label": item["label"],
                "data_type": item["data_type"],
                "is_required": item.get("is_required", False),
                "is_deprecated": False,
                "display_order": item.get("display_order", 0),
                "description": item.get("description", ""),
                "options": json.dumps(item.get("options", {})),
            }
        )
    if rows:
        op.bulk_insert(table, rows)


def downgrade() -> None:
    op.execute("DELETE FROM attribute_definitions")
