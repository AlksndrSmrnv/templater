"""Drop attribute_definitions.is_deprecated.

The attribute archiving flag is no longer used: attributes are now hard-deleted
from the UI instead of being marked deprecated. Drop the column.

Revision ID: 0004_drop_attr_is_deprecated
Revises: 0003_filled_templates
Create Date: 2026-05-29

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_drop_attr_is_deprecated"
down_revision = "0003_filled_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("attribute_definitions", "is_deprecated")


def downgrade() -> None:
    op.add_column(
        "attribute_definitions",
        sa.Column(
            "is_deprecated",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
