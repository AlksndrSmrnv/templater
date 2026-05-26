"""Add filled_templates table.

Stores snapshots of rendered (filled) message templates plus audit FKs to the
upstream template/clients/accounts/cards. All FKs use ``ON DELETE SET NULL`` so
deleting an upstream entity never blocks; UI falls back to the ``*_snapshot``
columns.

Revision ID: 0003_filled_templates
Revises: 0002_dedot_attribute_names
Create Date: 2026-05-26

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003_filled_templates"
down_revision = "0002_dedot_attribute_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "filled_templates",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("filled_content", sa.Text, nullable=False),
        sa.Column(
            "changed_locations",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "unresolved",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "message_template_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("message_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "template_name_snapshot",
            sa.String(255),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "sender_client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "sender_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "sender_card_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cards.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "receiver_client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "receiver_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "receiver_card_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cards.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "account_owner_client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "account_owner_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "account_owner_card_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cards.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "role_labels_snapshot",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_filled_templates_template_id",
        "filled_templates",
        ["message_template_id"],
    )
    op.create_index(
        "ix_filled_templates_created_at",
        "filled_templates",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_filled_templates_created_at", table_name="filled_templates")
    op.drop_index("ix_filled_templates_template_id", table_name="filled_templates")
    op.drop_table("filled_templates")
