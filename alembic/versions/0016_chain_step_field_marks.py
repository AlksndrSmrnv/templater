"""Chain step field marks: snapshot filled locations + click-to-bind reset buffer.

Adds two JSONB columns to ``request_chain_steps`` backing the coloured field
markup and the click-to-bind flow on the «Цепочка запросов» page:

- ``changed_locations`` — JSON-pointer / XML-path locations filled with concrete
  test data (snapshotted from the source filled template), coloured green;
- ``bindings`` — a reset buffer mapping a leaf location to its original text
  value before it was bound to a previous step's response field (the active
  ``{{ $N.path }}`` reference still lives inline in ``body``).

Revision ID: 0016_chain_step_field_marks
Revises: 0015_request_chains
Create Date: 2026-06-25

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0016_chain_step_field_marks"
down_revision = "0015_request_chains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "request_chain_steps",
        sa.Column(
            "changed_locations",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "request_chain_steps",
        sa.Column(
            "bindings",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("request_chain_steps", "bindings")
    op.drop_column("request_chain_steps", "changed_locations")
