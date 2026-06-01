"""Add llm_debug column to message_templates

Stores the last LLM request/response (system + user prompt and raw response)
captured during analysis, so the debug panel can show it after the fact — most
importantly for templates processed in bulk from the collections menu, where the
analysis happens in a separate request and its prompts would otherwise be lost.

Revision ID: 0007_template_llm_debug
Revises: 0006_reference_types_registry
Create Date: 2026-06-01

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007_template_llm_debug"
down_revision = "0006_reference_types_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "message_templates",
        sa.Column("llm_debug", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("message_templates", "llm_debug")
