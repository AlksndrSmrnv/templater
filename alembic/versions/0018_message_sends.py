"""Message send history: persist every «send» (request + response).

Creates ``message_sends`` — one row per send triggered from a filled template's
«Отправить» button or a chain step. Sending is still a stub seam (no real network
call), but the request envelope and the (mock) response are snapshotted so the UI
can show a full history table per object and the «last success / last error»
timestamps next to each send button.

All three source FKs (``filled_template_id`` / ``chain_id`` / ``chain_step_id``)
are ``ON DELETE SET NULL`` so a deleted source never blocks; the snapshot columns
keep the row meaningful regardless. ``source_kind`` records which one originated
the send.

Revision ID: 0018_message_sends
Revises: 0017_chain_step_role_ids
Create Date: 2026-06-29

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0018_message_sends"
down_revision = "0017_chain_step_role_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_sends",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column(
            "filled_template_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("filled_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "chain_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("request_chains.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "chain_step_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("request_chain_steps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name_snapshot", sa.String(length=255), nullable=False, server_default=sa.text("''")),
        sa.Column("http_method", sa.String(length=16), nullable=False, server_default=sa.text("''")),
        sa.Column("url", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column(
            "request_headers",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("request_body", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("ok", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("http_status", sa.Integer, nullable=True),
        sa.Column("status_code", sa.Integer, nullable=True),
        sa.Column(
            "response_headers",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("response_body", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("error_message", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_message_sends_filled", "message_sends", ["filled_template_id", "created_at"]
    )
    op.create_index("ix_message_sends_chain", "message_sends", ["chain_id", "created_at"])
    op.create_index("ix_message_sends_step", "message_sends", ["chain_step_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_message_sends_step", table_name="message_sends")
    op.drop_index("ix_message_sends_chain", table_name="message_sends")
    op.drop_index("ix_message_sends_filled", table_name="message_sends")
    op.drop_table("message_sends")
