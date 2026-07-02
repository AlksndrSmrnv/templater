"""Header preset HTTP method

Adds ``http_method`` to ``header_presets`` so a preset can carry a request type
(POST/GET/…) that is copied onto a template when the preset is applied, where it
renders via the existing method plate. Empty string = not set, mirroring
``message_templates.http_method``.

Revision ID: 0021_header_preset_http_method
Revises: 0020_unified_tree_order
Create Date: 2026-07-02

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0021_header_preset_http_method"
down_revision = "0020_unified_tree_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "header_presets",
        sa.Column("http_method", sa.String(length=16), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("header_presets", "http_method")
