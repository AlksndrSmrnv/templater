"""Deduplicate dot semantics in attribute names.

Revision ID: 0002_dedot_attribute_names
Revises: 0001_init
Create Date: 2026-05-22

"""
from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa

from alembic import op

revision = "0002_dedot_attribute_names"
down_revision = "0001_init"
branch_labels = None
depends_on = None

CORE_ENTITY_TABLES = {
    "client": "clients",
    "account": "accounts",
    "card": "cards",
}
TOKEN_ROLES = ("sender", "receiver", "accountOwner")
DEFAULT_DOWNGRADES = (
    ("client", "passportSeries", "passport.series"),
    ("client", "passportNumber", "passport.number"),
)

Rename = tuple[str, str, str]


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT entity_type, name
            FROM attribute_definitions
            WHERE name LIKE '%.%'
            ORDER BY entity_type, name
            """
        )
    ).mappings()
    renames = [
        (str(row["entity_type"]), str(row["name"]), _dedot_name(str(row["name"])))
        for row in rows
    ]
    _apply_renames(bind, renames)


def downgrade() -> None:
    # Only the known default seed names can be reversed automatically. Custom
    # dotted names that were migrated during upgrade cannot be inferred safely.
    bind = op.get_bind()
    _apply_renames(bind, DEFAULT_DOWNGRADES)


def _dedot_name(name: str) -> str:
    head, *tail = name.split(".")
    return head + "".join(segment[:1].upper() + segment[1:] for segment in tail)


def _apply_renames(bind: sa.engine.Connection, renames: Iterable[Rename]) -> None:
    renames = list(renames)
    for entity_type, old_name, new_name in renames:
        bind.execute(
            sa.text(
                """
                UPDATE attribute_definitions
                SET name = :new_name
                WHERE entity_type = :entity_type AND name = :old_name
                """
            ),
            {"entity_type": entity_type, "old_name": old_name, "new_name": new_name},
        )
        _rename_jsonb_attribute_key(
            bind,
            entity_type=entity_type,
            old_name=old_name,
            new_name=new_name,
        )
    _rewrite_template_tokens(bind, renames)


def _rename_jsonb_attribute_key(
    bind: sa.engine.Connection,
    *,
    entity_type: str,
    old_name: str,
    new_name: str,
) -> None:
    table_name = CORE_ENTITY_TABLES.get(entity_type)
    params = {"old_name": old_name, "new_name": new_name}
    if table_name is not None:
        bind.execute(
            sa.text(
                f"""
                UPDATE {table_name}
                SET attributes = (attributes - :old_name)
                    || jsonb_build_object(:new_name, attributes -> :old_name)
                WHERE attributes ? :old_name
                """
            ),
            params,
        )
        return

    bind.execute(
        sa.text(
            """
            UPDATE reference_values
            SET attributes = (attributes - :old_name)
                || jsonb_build_object(:new_name, attributes -> :old_name)
            WHERE entity_type = :entity_type AND attributes ? :old_name
            """
        ),
        {**params, "entity_type": entity_type},
    )


def _rewrite_template_tokens(bind: sa.engine.Connection, renames: Iterable[Rename]) -> None:
    path_replacements: list[tuple[str, str]] = []
    for entity_type, old_name, new_name in renames:
        for role in TOKEN_ROLES:
            old_path = _token_path(role, entity_type, old_name)
            new_path = _token_path(role, entity_type, new_name)
            if old_path is not None and new_path is not None:
                path_replacements.append((old_path, new_path))

    path_replacements.sort(key=lambda item: len(item[0]), reverse=True)
    for old_path, new_path in path_replacements:
        bind.execute(
            sa.text(
                """
                UPDATE message_templates
                SET content = replace(content, :old_path, :new_path),
                    placeholders = replace(placeholders::text, :old_path, :new_path)::jsonb
                WHERE content LIKE :like_path OR placeholders::text LIKE :like_path
                """
            ),
            {
                "old_path": old_path,
                "new_path": new_path,
                "like_path": f"%{old_path}%",
            },
        )


def _token_path(role: str, entity_type: str, name: str) -> str | None:
    if entity_type == "client":
        return f"{role}.{name}"
    if entity_type == "account":
        return f"{role}.account.{name}"
    if entity_type == "card":
        return f"{role}.card.{name}"
    return None
