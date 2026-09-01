# backend/alembic/versions/b3c4d5e6f7a8_add_vm_applied_environment.py
"""Add applied_environment column to VMs to track live container env state

`vms.environment` is the desired state and is editable at any time. Docker bakes
environment into a container at create time and never re-reads it on restart, so
a running container can silently diverge from the desired state. This column
records what the live container was actually created with; the difference drives
the "pending changes" indicator and is reconciled by POST /vms/{id}/apply-config.

No backfill: for a container created before this column existed there is no
record of what it was built with, and a migration cannot ask Docker. NULL means
exactly that — unknown — which reads as "everything pending" and prompts the
recreate that makes the container's env match the row. Assuming instead that such
a container is in sync is unsound, and was wrong in practice: the first VM this
shipped against had all five target variables baked in empty while the row held
the correct values.

Revision ID: b3c4d5e6f7a8
Revises: a1b2merge0001
Create Date: 2026-08-18 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, None] = "a1b2merge0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vms", sa.Column("applied_environment", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("vms", "applied_environment")
