"""Add the feedback table

Customer feedback from preprod: platform ideas and defects, and problems with a guide or a
range. `source`/`source_ref` are indexed columns rather than prose so that everything raised
against one walkthrough is a query, not a read-through -- the lesson COSMOS wrote into its own
migration for the same feature.

Revision ID: f1e2d3c4b5a6
Revises: c4a1b2d3e5f7
Create Date: 2026-09-25 10:20:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1e2d3c4b5a6"
down_revision: Union[str, None] = "c4a1b2d3e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feedback",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("author_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="bug"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="open"),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="app"),
        sa.Column("source_ref", sa.String(length=200), nullable=True),
        # SET NULL, not CASCADE: a range is torn down at the end of every exercise and the
        # report about it has to outlive it.
        sa.Column(
            "range_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("ranges.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "content_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("content.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("external_ref", sa.String(length=100), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_feedback_author_id", "feedback", ["author_id"])
    op.create_index("ix_feedback_kind", "feedback", ["kind"])
    op.create_index("ix_feedback_status", "feedback", ["status"])
    op.create_index("ix_feedback_range_id", "feedback", ["range_id"])
    op.create_index("ix_feedback_content_id", "feedback", ["content_id"])
    # Prefix-matchable, so one guide's feedback comes back as a group.
    op.create_index("ix_feedback_source_ref", "feedback", ["source", "source_ref"])
    op.create_index("ix_feedback_status_created", "feedback", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_feedback_status_created", table_name="feedback")
    op.drop_index("ix_feedback_source_ref", table_name="feedback")
    op.drop_index("ix_feedback_content_id", table_name="feedback")
    op.drop_index("ix_feedback_range_id", table_name="feedback")
    op.drop_index("ix_feedback_status", table_name="feedback")
    op.drop_index("ix_feedback_kind", table_name="feedback")
    op.drop_index("ix_feedback_author_id", table_name="feedback")
    op.drop_table("feedback")
