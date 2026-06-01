"""Add folders column to collections

Persists the explicit folder structure of a collection (a list of folder paths,
each path a list of segments). Templates carry their own ``folder_path`` for
membership, but that can't represent an *empty* folder — created or renamed
folders are stored here so they survive tree rebuilds and so rename/delete have
an authoritative target.

Revision ID: 0008_collection_folders
Revises: 0007_template_llm_debug
Create Date: 2026-06-01

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0008_collection_folders"
down_revision = "0007_template_llm_debug"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column(
            "folders",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("collections", "folders")
