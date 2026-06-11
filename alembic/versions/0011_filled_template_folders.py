"""Folder placement and execution snapshots for filled templates

Filled templates get the same materialised-path folder storage as message
templates (``folder_path`` + ``display_order``; explicit empty folders live in
the ``filled_root_folders`` app setting, which needs no migration). The
``*_snapshot`` trio copies the source template's HTTP method/URL/headers at
save time so a filled template stays runnable (future "send request" feature)
even after the source template is edited or deleted.

Revision ID: 0011_filled_template_folders
Revises: 0010_projects
Create Date: 2026-06-11

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0011_filled_template_folders"
down_revision = "0010_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "filled_templates",
        sa.Column(
            "folder_path",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "filled_templates",
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "filled_templates",
        sa.Column("http_method_snapshot", sa.String(length=16), nullable=False, server_default=""),
    )
    op.add_column(
        "filled_templates",
        sa.Column("url_snapshot", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "filled_templates",
        sa.Column(
            "headers_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("filled_templates", "headers_snapshot")
    op.drop_column("filled_templates", "url_snapshot")
    op.drop_column("filled_templates", "http_method_snapshot")
    op.drop_column("filled_templates", "display_order")
    op.drop_column("filled_templates", "folder_path")
