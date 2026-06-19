"""Collection jobs: background LLM batch processing with progress.

Creates the ``collection_jobs`` table tracking one background LLM-processing
job per collection. A job carries ``status`` (pending|running|done|failed) and
per-template counters (``processed``/``skipped``/``failed``) that the frontend
polls via ``GET /collections/{collection_id}/jobs/{job_id}`` to render a
progress bar. On restart any still-pending/running row is reconciled to
``failed`` (the in-process task is gone with the process). ``collection_id``
is CASCADE so deleting a collection cleans up its job rows too.

Revision ID: 0014_collection_jobs
Revises: 0013_access_groups
Create Date: 2026-06-19

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014_collection_jobs"
down_revision = "0013_access_groups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "collection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("total", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("processed", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("skipped", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("failed", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index(
        "ix_collection_jobs_collection_status",
        "collection_jobs",
        ["collection_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_collection_jobs_collection_status", table_name="collection_jobs"
    )
    op.drop_table("collection_jobs")
