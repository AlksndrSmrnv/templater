"""Projects: tag every template with exactly one project

Creates the ``projects`` table (name + highlight color), seeds the service
project «Без проекта», backfills all existing templates to it and makes
``message_templates.project_id`` NOT NULL with a RESTRICT FK. Filled templates
get project name/color snapshot columns (same survival semantics as
``template_name_snapshot``), backfilled from the live template→project link.

Revision ID: 0010_projects
Revises: 0009_drop_references
Create Date: 2026-06-11

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0010_projects"
down_revision = "0009_drop_references"
branch_labels = None
depends_on = None

# Fixed id for the seed project so the backfill UPDATE can reference it.
DEFAULT_PROJECT_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_PROJECT_NAME = "Без проекта"
DEFAULT_PROJECT_COLOR = "#9E9E9E"


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("color", sa.String(length=16), nullable=False, server_default=DEFAULT_PROJECT_COLOR),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", name="uq_projects_name"),
    )
    op.execute(
        sa.text(
            "INSERT INTO projects (id, name, color) VALUES (:id, :name, :color)"
        ).bindparams(id=DEFAULT_PROJECT_ID, name=DEFAULT_PROJECT_NAME, color=DEFAULT_PROJECT_COLOR)
    )

    op.add_column(
        "message_templates",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        sa.text("UPDATE message_templates SET project_id = :id").bindparams(id=DEFAULT_PROJECT_ID)
    )
    op.alter_column("message_templates", "project_id", nullable=False)
    op.create_index("ix_message_templates_project_id", "message_templates", ["project_id"])
    op.create_foreign_key(
        "fk_message_templates_project_id",
        "message_templates",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.add_column(
        "filled_templates",
        sa.Column("project_name_snapshot", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "filled_templates",
        sa.Column("project_color_snapshot", sa.String(length=16), nullable=False, server_default=""),
    )
    # Backfill snapshots through the still-live template→project link; orphaned
    # filled templates (template already deleted) keep "" and the UI shows «—».
    op.execute(
        """
        UPDATE filled_templates ft
        SET project_name_snapshot = p.name,
            project_color_snapshot = p.color
        FROM message_templates mt
        JOIN projects p ON p.id = mt.project_id
        WHERE ft.message_template_id = mt.id
        """
    )


def downgrade() -> None:
    op.drop_column("filled_templates", "project_color_snapshot")
    op.drop_column("filled_templates", "project_name_snapshot")
    op.drop_constraint("fk_message_templates_project_id", "message_templates", type_="foreignkey")
    op.drop_index("ix_message_templates_project_id", table_name="message_templates")
    op.drop_column("message_templates", "project_id")
    op.drop_table("projects")
