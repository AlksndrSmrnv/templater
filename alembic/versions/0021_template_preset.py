"""Template → preset link for real REST sending.

Adds a nullable ``preset_id`` FK to ``message_templates`` pointing at
``header_presets``. Applying a preset already *copies* its url + headers onto the
template; the id is now kept too so the send flow can tell a template "has a
configured preset" and reach that preset's browser-held connection (client
certs) — driving real vs. mock sending. ``ON DELETE SET NULL`` so deleting a
preset never blocks and just drops the template back to the mock path.

Existing templates keep ``preset_id = NULL`` (mock) until a preset is re-applied.

Revision ID: 0021_template_preset
Revises: 0020_unified_tree_order
Create Date: 2026-07-01

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0021_template_preset"
down_revision = "0020_unified_tree_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "message_templates",
        sa.Column("preset_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_message_templates_preset_id",
        "message_templates",
        "header_presets",
        ["preset_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_message_templates_preset_id", "message_templates", type_="foreignkey"
    )
    op.drop_column("message_templates", "preset_id")
