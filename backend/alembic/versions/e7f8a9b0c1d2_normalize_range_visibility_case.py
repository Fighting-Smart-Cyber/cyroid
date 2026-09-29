"""Normalize range.visibility to lowercase enum values

The column was added as String(20) with server_default="private" -- the enum
VALUE. But the ORM mapped it as Mapped[RangeVisibility] with no explicit type,
and SQLAlchemy's inferred Enum persists member NAMES. So the same column held
two spellings depending on who wrote the row:

    server_default (migration, raw SQL)  ->  "private"
    ORM insert                           ->  "PRIVATE"

Reading a lowercase row raised

    LookupError: 'private' is not among the defined enum values

which failed the whole GET /ranges listing, not merely the offending row -- a
user could not see any range, including ones they owned.

The model now pins storage to the value via values_callable. This backfills the
rows the ORM wrote in the other spelling so both agree.

Revision ID: e7f8a9b0c1d2
Revises: d1a2b3c4e5f6
"""

from alembic import op

revision = "e7f8a9b0c1d2"
down_revision = "d1a2b3c4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Case-only rewrite; the set of distinct values is unchanged.
    op.execute("""
        UPDATE ranges
           SET visibility = lower(visibility)
         WHERE visibility IS NOT NULL
           AND visibility <> lower(visibility)
        """)


def downgrade() -> None:
    # Deliberately not reversed. Upper-casing these again would restore the
    # defect, and the lowercase spelling is valid under both the old and new
    # model -- the old one simply could not read it.
    pass
