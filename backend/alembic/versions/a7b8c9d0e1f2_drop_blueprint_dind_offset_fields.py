"""Drop the DinD-era subnet-allocation fields from range_blueprints -- COSMOS PG-122

`base_subnet_prefix` and `next_offset` allocated a subnet per blueprint instance: each deploy took
the counter, incremented it, and the range's addresses were shifted by that many second octets.
DinD isolation made the allocation pointless -- every range already has its own network namespace
and can reuse identical address space -- and both columns were marked deprecated without being
removed.

They were not, however, inert. `create_range_from_blueprint` accepted the offset and ignored it,
while a redeploy of an instance applied it, so an instance's networks silently moved the first time
it was redeployed. Removing the columns removes that divergence with them.

`range_instances.subnet_offset` stays: the UI numbers a blueprint's instances with it. It is now a
label counted from the instance table rather than a subnet allocated from a counter, so it also
gains the default it never had.

Downgrade restores the columns and their old defaults. It cannot restore the counter's value --
nothing records how many instances a blueprint had allocated -- so it seeds `next_offset` from the
number of instances that exist, which is what the counter approximated.

Revision ID: a7b8c9d0e1f2
Revises: f1e2d3c4b5a6
Create Date: 2026-09-28 12:55:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "f1e2d3c4b5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("range_blueprints", "base_subnet_prefix")
    op.drop_column("range_blueprints", "next_offset")
    op.alter_column("range_instances", "subnet_offset", server_default="0")


def downgrade() -> None:
    op.alter_column("range_instances", "subnet_offset", server_default=None)
    op.add_column(
        "range_blueprints",
        sa.Column(
            "base_subnet_prefix",
            sa.String(length=20),
            nullable=True,
            server_default="10.0.0.0/8",
        ),
    )
    op.add_column(
        "range_blueprints",
        sa.Column("next_offset", sa.Integer(), nullable=True, server_default="0"),
    )
    op.execute("""
        UPDATE range_blueprints b
           SET next_offset = (
               SELECT COUNT(*) FROM range_instances i WHERE i.blueprint_id = b.id
           )
        """)
