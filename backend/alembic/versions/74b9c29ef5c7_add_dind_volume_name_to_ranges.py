# backend/alembic/script.py.mako
"""add dind_volume_name to ranges

Revision ID: 74b9c29ef5c7
Revises: d5e6f7a8b9c0
Create Date: 2026-08-26 21:13:16.634801

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "74b9c29ef5c7"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ranges", sa.Column("dind_volume_name", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("ranges", "dind_volume_name")
