"""Access groups: password-protected vaults for sensitive test data

Creates the ``access_groups`` table (name + color badge + salted PBKDF2 password
hash) and tags ``clients`` and ``filled_templates`` with an optional
``group_id``. A ``NULL`` tag means public/visible to everyone, so existing rows
need no backfill. Both FKs are RESTRICT — deleting a group is refused at the
service level while any client or filled template references it, so a group can
never be removed in a way that silently exposes private data. The filled-template
group name/color are also snapshotted (like the project badge) so the badge
survives the group being deleted.

Revision ID: 0013_access_groups
Revises: 0012_header_presets
Create Date: 2026-06-16

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013_access_groups"
down_revision = "0012_header_presets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("color", sa.String(length=16), nullable=False, server_default="#9E9E9E"),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", name="uq_access_groups_name"),
    )

    op.add_column(
        "clients",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_clients_group_id",
        "clients",
        "access_groups",
        ["group_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_clients_group_id", "clients", ["group_id"])

    op.add_column(
        "filled_templates",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "filled_templates",
        sa.Column("group_name_snapshot", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "filled_templates",
        sa.Column("group_color_snapshot", sa.String(length=16), nullable=False, server_default=""),
    )
    op.create_foreign_key(
        "fk_filled_templates_group_id",
        "filled_templates",
        "access_groups",
        ["group_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_filled_templates_group_id", "filled_templates", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_filled_templates_group_id", table_name="filled_templates")
    op.drop_constraint("fk_filled_templates_group_id", "filled_templates", type_="foreignkey")
    op.drop_column("filled_templates", "group_color_snapshot")
    op.drop_column("filled_templates", "group_name_snapshot")
    op.drop_column("filled_templates", "group_id")

    op.drop_index("ix_clients_group_id", table_name="clients")
    op.drop_constraint("fk_clients_group_id", "clients", type_="foreignkey")
    op.drop_column("clients", "group_id")

    op.drop_table("access_groups")
