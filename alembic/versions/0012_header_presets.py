"""Header presets: reusable endpoint URL + headers tagged by project

Creates the ``header_presets`` table. Each preset holds a standard ``url`` and a
JSONB list of headers, tagged with exactly one project (same label used on
templates). The FK is CASCADE: a project can only be deleted once no templates
reference it, and at that point its presets are disposable config and go with it.

Revision ID: 0012_header_presets
Revises: 0011_filled_template_folders
Create Date: 2026-06-16

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0012_header_presets"
down_revision = "0011_filled_template_folders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "header_presets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "headers",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_header_presets_project_id", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("project_id", "name", name="uq_header_presets_project_name"),
    )
    op.create_index("ix_header_presets_project_id", "header_presets", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_header_presets_project_id", table_name="header_presets")
    op.drop_table("header_presets")
