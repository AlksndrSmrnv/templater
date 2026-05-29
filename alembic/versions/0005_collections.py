"""Add collections + collection/headers columns on message_templates.

Introduces the ``collections`` table (imported Postman/Insomnia/… collections)
and extends ``message_templates`` with collection membership, folder path,
HTTP headers, method, URL and display order so an imported request can be
stored as a full template.

Revision ID: 0005_collections
Revises: 0004_drop_attr_is_deprecated
Create Date: 2026-05-29

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0005_collections"
down_revision = "0004_drop_attr_is_deprecated"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collections",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("source", sa.String(32), nullable=False, server_default=sa.text("'postman'")),
        sa.Column("source_format", sa.String(64), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "variables",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.add_column(
        "message_templates",
        sa.Column(
            "collection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("collections.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "message_templates",
        sa.Column(
            "folder_path",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "message_templates",
        sa.Column(
            "headers",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "message_templates",
        sa.Column("http_method", sa.String(16), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "message_templates",
        sa.Column("url", sa.Text, nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "message_templates",
        sa.Column("display_order", sa.Integer, nullable=False, server_default=sa.text("0")),
    )
    op.create_index(
        "ix_message_templates_collection_id",
        "message_templates",
        ["collection_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_message_templates_collection_id", table_name="message_templates")
    op.drop_column("message_templates", "display_order")
    op.drop_column("message_templates", "url")
    op.drop_column("message_templates", "http_method")
    op.drop_column("message_templates", "headers")
    op.drop_column("message_templates", "folder_path")
    op.drop_column("message_templates", "collection_id")
    op.drop_table("collections")
