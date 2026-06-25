"""Request chains: ordered REST-request chains built from filled templates.

Creates two tables backing the «Цепочка запросов» entity shown in the
«Заполненные шаблоны» workspace:

- ``request_chains`` — a chain that lives in the same folder tree as filled
  templates (materialised ``folder_path`` + ``display_order``) with optional
  access-group visibility (``group_id``, NULL = public);
- ``request_chain_steps`` — ordered steps, each snapshotting one filled
  template's request envelope (method/url/headers/body/format) plus an editable
  ``mock_response``. ``chain_id`` is CASCADE so deleting a chain removes its
  steps; ``filled_template_id`` is SET NULL so deleting the source filled
  template never blocks (the snapshot keeps the step runnable).

Revision ID: 0015_request_chains
Revises: 0014_collection_jobs
Create Date: 2026-06-25

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0015_request_chains"
down_revision = "0014_collection_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "request_chains",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "folder_path",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("display_order", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("access_groups.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("group_name_snapshot", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("group_color_snapshot", sa.String(length=16), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_request_chains_group_id", "request_chains", ["group_id"])
    op.create_index("ix_request_chains_created_at", "request_chains", ["created_at"])

    op.create_table(
        "request_chain_steps",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "chain_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("request_chains.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "filled_template_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("filled_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name_snapshot", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("format", sa.String(length=16), nullable=False, server_default=sa.text("'json'")),
        sa.Column("http_method_snapshot", sa.String(length=16), nullable=False, server_default=sa.text("''")),
        sa.Column("url_snapshot", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column(
            "headers_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("body", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("mock_response", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_request_chain_steps_chain_id", "request_chain_steps", ["chain_id"])
    # Unique step order per chain. DEFERRABLE INITIALLY DEFERRED so a single
    # transaction can renumber positions 0..n-1 (reorder/remove) without
    # tripping on transient mid-flush collisions; the check runs at COMMIT.
    op.create_unique_constraint(
        "uq_request_chain_steps_chain_position",
        "request_chain_steps",
        ["chain_id", "position"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_request_chain_steps_chain_position",
        "request_chain_steps",
        type_="unique",
    )
    op.drop_index("ix_request_chain_steps_chain_id", table_name="request_chain_steps")
    op.drop_table("request_chain_steps")
    op.drop_index("ix_request_chains_created_at", table_name="request_chains")
    op.drop_index("ix_request_chains_group_id", table_name="request_chains")
    op.drop_table("request_chains")
