# backend/alembic/versions/c4d5e6f7a8b9_clear_backfilled_applied_environment.py
"""Clear applied_environment values written by b3c4d5e6f7a8's backfill

b3c4d5e6f7a8 originally backfilled `applied_environment = environment` for every
provisioned VM, on the assumption that an existing container had been created
from the environment currently on its row. That assumption is unsound — a
container created before the environment was edited holds the values it was
built with, not the ones since saved — and it was false on the first VM it ran
against, whose container had all five target variables baked in empty while the
row held the correct values. The effect is the reverse of the feature's purpose:
the pending badge reports "in sync" for precisely the container that is stale.

This resets the column to NULL, which means "unknown". Unknown reads as
"everything pending", so the badge prompts the recreate that actually makes the
container match the row.

Cost of the blunt reset: a VM whose config was legitimately applied between that
migration and this one loses that record and shows as pending once. Re-applying
restores it. That is a strictly safer error than the one being corrected — it
over-reports staleness instead of hiding it.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-08-18 20:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE vms SET applied_environment = NULL")


def downgrade() -> None:
    # Deliberately not reinstated: the backfill this reverses was incorrect, and
    # recreating it would restore the false "in sync" state.
    pass
