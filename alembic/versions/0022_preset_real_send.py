"""Preset flag for real browser-side sending.

Adds ``header_presets.use_real_send``: when a template's preset carries this
flag, the browser sends the request for real (directly via fetch, client cert
taken from the OS keystore); otherwise the send is mocked. Replaces the old
browser-held sessionStorage "connection" (client-cert upload) as the real-vs-mock
switch — cert material никогда не проходит через приложение.

Existing presets default to ``false`` (mock), matching the old behaviour of a
preset without a configured connection.

Revision ID: 0022_preset_real_send
Revises: 0021_template_preset
Create Date: 2026-07-02

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022_preset_real_send"
down_revision = "0021_template_preset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "header_presets",
        sa.Column(
            "use_real_send",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("header_presets", "use_real_send")
