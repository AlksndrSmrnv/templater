"""Repair attribute_definitions.options rows that were stored as JSONB strings

Earlier versions of 0002_seed_attribute_schema inserted ``options`` as
``json.dumps(...)``, which combined with SQLAlchemy's JSON bind processor
double-serialized the value: in the database it ended up as a JSONB string
(``"{...}"``) instead of a JSONB object. This migration unwraps any such
rows back into proper JSONB objects.

Revision ID: 0003_repair_options_jsonb
Revises: 0002_seed_attribute_schema
Create Date: 2026-05-19

"""
from __future__ import annotations

from alembic import op

revision = "0003_repair_options_jsonb"
down_revision = "0002_seed_attribute_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE attribute_definitions
        SET options = (options #>> '{}')::jsonb
        WHERE jsonb_typeof(options) = 'string';
        """
    )


def downgrade() -> None:
    # Re-wrap as string is destructive and meaningless; no-op.
    pass
