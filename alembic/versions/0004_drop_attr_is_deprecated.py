"""Drop attribute_definitions.is_deprecated.

The attribute archiving flag is no longer used: attributes are now hard-deleted
from the UI instead of being marked deprecated.

Previously-archived attributes (is_deprecated = true) are hard-deleted before the
column is dropped — otherwise dropping the flag would silently "un-archive" them
and make them reappear in forms/catalogs/settings. Their stored values in entity
records' JSON are left untouched, matching the new delete-only-the-definition model.

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
    # Drop archived attribute definitions first so removing the flag does not
    # resurrect them. Values stored under these attributes in entity records' JSON
    # are intentionally kept (delete only the definition).
    op.execute(sa.text("DELETE FROM attribute_definitions WHERE is_deprecated"))
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
