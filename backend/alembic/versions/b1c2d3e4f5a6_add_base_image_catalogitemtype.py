"""add BASE_IMAGE to catalogitemtype

The model renamed CatalogItemType.TEMPLATE to BASE_IMAGE, but the PostgreSQL
enum was never migrated with it — it still carries TEMPLATE and has no
BASE_IMAGE. Installing any base image from a catalog therefore fails at the
insert with:

    invalid input value for enum catalogitemtype: "BASE_IMAGE"

Nothing caught it because the test suite runs on SQLite, which does not enforce
enum membership. Found by installing a catalog blueprint through the UI against
a real Postgres.

TEMPLATE is left in place: PostgreSQL cannot drop an enum value without
recreating the type, and rows written before the rename may still reference it.

Revision ID: b1c2d3e4f5a6
Revises: e7f8a9b0c1d2
Create Date: 2026-09-15 16:45:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Uppercase to match the existing labels: SQLAlchemy stores the enum's
    # member name, not its value.
    op.execute("ALTER TYPE catalogitemtype ADD VALUE IF NOT EXISTS 'BASE_IMAGE'")


def downgrade() -> None:
    # PostgreSQL cannot remove an enum value without recreating the type, and
    # dropping it would orphan any row that uses it.
    pass
