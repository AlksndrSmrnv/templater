"""Chain project snapshot: every step of a chain must share one project.

Adds ``project_name_snapshot`` / ``project_color_snapshot`` to ``request_chains``,
mirroring the existing group invariant. The first step sets the chain's project
(from the filled template's project snapshot — there is no project_id to FK), a
step from a different project is rejected, and removing the last step clears it.

Existing chains are backfilled from their earliest step's source filled template
so the new same-project rule doesn't retroactively block adding to them. Steps
whose source filled template was deleted contribute nothing (stay "").

Revision ID: 0019_chain_project
Revises: 0018_message_sends
Create Date: 2026-06-29

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0019_chain_project"
down_revision = "0018_message_sends"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "request_chains",
        sa.Column(
            "project_name_snapshot",
            sa.String(length=255),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "request_chains",
        sa.Column(
            "project_color_snapshot",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    # Backfill from each chain's earliest step's source filled template, so the
    # same-project rule applied on the next add reflects what's already there.
    op.execute(
        """
        UPDATE request_chains c
        SET project_name_snapshot  = ft.project_name_snapshot,
            project_color_snapshot = ft.project_color_snapshot
        FROM request_chain_steps s
        JOIN filled_templates ft ON ft.id = s.filled_template_id
        WHERE s.chain_id = c.id
          AND s.position = (
              SELECT MIN(s2.position)
              FROM request_chain_steps s2
              WHERE s2.chain_id = c.id
                AND s2.filled_template_id IS NOT NULL
          )
        """
    )


def downgrade() -> None:
    op.drop_column("request_chains", "project_color_snapshot")
    op.drop_column("request_chains", "project_name_snapshot")
