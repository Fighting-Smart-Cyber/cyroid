"""Range visibility: private by default, shared by name or tag

Visibility used to be inferred from tags, and a range with none was treated as
public. Every range anyone created was therefore visible to every non-student
account -- a default nobody chose. This makes it explicit and flips it closed.

Existing rows become PRIVATE. That is deliberate: the alternative preserves
today's behaviour by making everything public, which is the permissive default
this exists to remove. Re-sharing a handful of ranges now is cheap; discovering
later that everything was still open is not.

Revision ID: d1a2b3c4e5f6
Revises: c9e4f5a6b7d8
"""

import sqlalchemy as sa
from alembic import op

revision = "d1a2b3c4e5f6"
down_revision = "c9e4f5a6b7d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so rows existing at migration time get PRIVATE without a
    # second UPDATE, and so a row inserted by anything that does not know about
    # the column still lands closed rather than open.
    op.add_column(
        "ranges",
        sa.Column("visibility", sa.String(length=20), nullable=False, server_default="private"),
    )

    op.create_table(
        "range_shares",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("range_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("granted_by", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["range_id"], ["ranges.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"], ondelete="SET NULL"),
        # One grant per person per range; re-sharing is not a second row.
        sa.UniqueConstraint("range_id", "user_id", name="uq_range_share"),
    )
    op.create_index("ix_range_shares_range_id", "range_shares", ["range_id"])
    op.create_index("ix_range_shares_user_id", "range_shares", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_range_shares_user_id", table_name="range_shares")
    op.drop_index("ix_range_shares_range_id", table_name="range_shares")
    op.drop_table("range_shares")
    op.drop_column("ranges", "visibility")
