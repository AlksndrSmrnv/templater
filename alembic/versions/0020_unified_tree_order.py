"""Unified per-folder display_order for filled templates and request chains.

Filled templates and request chains previously kept **independent**
``display_order`` sequences per folder — the tree always rendered templates
first, then chains, so a chain could only ever appear *below* the folder's
templates regardless of where the user dropped it.

This migration merges the two sequences into one shared per-folder ordering:
within each folder, filled templates come first (preserving their existing
relative order), then chains (preserving theirs), renumbered 0..n-1 across
both tables. The visible order after the migration is unchanged; the new shared
sequence lets a chain be dropped anywhere among the folder's templates.

Revision ID: 0020_unified_tree_order
Revises: 0019_chain_project
Create Date: 2026-06-30

"""
from __future__ import annotations

from alembic import op

revision = "0020_unified_tree_order"
down_revision = "0019_chain_project"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Renumber both tables within each folder as one shared sequence:
    # filled templates first (kind_rank 0), then chains (kind_rank 1),
    # preserving the previous (display_order, created_at) order within each
    # kind. JSONB folder_path is compared by value, so [] == [] across rows.
    op.execute(
        """
        WITH merged AS (
            SELECT id, folder_path, 0 AS kind_rank, display_order, created_at
            FROM filled_templates
            UNION ALL
            SELECT id, folder_path, 1 AS kind_rank, display_order, created_at
            FROM request_chains
        ),
        renumbered AS (
            SELECT id, kind_rank, folder_path,
                   (ROW_NUMBER() OVER (
                        PARTITION BY folder_path
                        ORDER BY kind_rank, display_order, created_at
                   ) - 1)::int AS new_order
            FROM merged
        )
        UPDATE filled_templates ft
        SET display_order = r.new_order
        FROM renumbered r
        WHERE ft.id = r.id AND r.kind_rank = 0
        """
    )
    op.execute(
        """
        WITH merged AS (
            SELECT id, folder_path, 0 AS kind_rank, display_order, created_at
            FROM filled_templates
            UNION ALL
            SELECT id, folder_path, 1 AS kind_rank, display_order, created_at
            FROM request_chains
        ),
        renumbered AS (
            SELECT id, kind_rank, folder_path,
                   (ROW_NUMBER() OVER (
                        PARTITION BY folder_path
                        ORDER BY kind_rank, display_order, created_at
                   ) - 1)::int AS new_order
            FROM merged
        )
        UPDATE request_chains rc
        SET display_order = r.new_order
        FROM renumbered r
        WHERE rc.id = r.id AND r.kind_rank = 1
        """
    )


def downgrade() -> None:
    # Restore the independent per-kind 0..n-1 sequences: each kind keeps its
    # relative order, but chains are re-offset so they start at 0 again (their
    # display_order was shifted past the folder's filled templates by upgrade()).
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   (ROW_NUMBER() OVER (
                        PARTITION BY folder_path
                        ORDER BY display_order, created_at
                   ) - 1)::int AS new_order
            FROM filled_templates
        )
        UPDATE filled_templates ft
        SET display_order = r.new_order
        FROM ranked r
        WHERE ft.id = r.id
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   (ROW_NUMBER() OVER (
                        PARTITION BY folder_path
                        ORDER BY display_order, created_at
                   ) - 1)::int AS new_order
            FROM request_chains
        )
        UPDATE request_chains rc
        SET display_order = r.new_order
        FROM ranked r
        WHERE rc.id = r.id
        """
    )
