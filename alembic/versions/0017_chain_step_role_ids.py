"""Chain step role bindings: per-step sender/receiver/accountOwner ids + labels.

Adds the nine role FK columns (client/account/card for sender, receiver and
account owner) plus ``role_labels_snapshot`` to ``request_chain_steps``,
mirroring ``filled_templates``. They are populated from the source filled
template when a step is added, and let the «Заменить клиента» menu re-point a
role and re-render the step body from the source message template. All FKs use
``ON DELETE SET NULL`` so deleting an upstream client/account/card never blocks.

Existing rows backfill to NULL / ``{}`` — their client-switch controls stay
disabled (the source role ids are unknown) until the step is re-added.

Revision ID: 0017_chain_step_role_ids
Revises: 0016_chain_step_field_marks
Create Date: 2026-06-26

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0017_chain_step_role_ids"
down_revision = "0016_chain_step_field_marks"
branch_labels = None
depends_on = None

_ROLE_COLUMNS = (
    ("sender_client_id", "clients"),
    ("sender_account_id", "accounts"),
    ("sender_card_id", "cards"),
    ("receiver_client_id", "clients"),
    ("receiver_account_id", "accounts"),
    ("receiver_card_id", "cards"),
    ("account_owner_client_id", "clients"),
    ("account_owner_account_id", "accounts"),
    ("account_owner_card_id", "cards"),
)


def upgrade() -> None:
    for column, target in _ROLE_COLUMNS:
        op.add_column(
            "request_chain_steps",
            sa.Column(column, postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            f"fk_request_chain_steps_{column}",
            "request_chain_steps",
            target,
            [column],
            ["id"],
            ondelete="SET NULL",
        )
    op.add_column(
        "request_chain_steps",
        sa.Column(
            "role_labels_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("request_chain_steps", "role_labels_snapshot")
    for column, _target in reversed(_ROLE_COLUMNS):
        op.drop_constraint(
            f"fk_request_chain_steps_{column}",
            "request_chain_steps",
            type_="foreignkey",
        )
        op.drop_column("request_chain_steps", column)
