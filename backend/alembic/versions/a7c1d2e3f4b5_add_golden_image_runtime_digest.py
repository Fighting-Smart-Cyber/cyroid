"""add golden_images.runtime_image_digest

Revision ID: a7c1d2e3f4b5
Revises: 99b0004c8d8e
Create Date: 2026-08-28 11:35:00.000000

A disk-based golden image is an installed OS, and an installed OS expects the
virtual hardware it was installed on. The dockur runtime is pulled by the
:latest tag, so it drifts: a range that cached dockurr/windows four weeks ago
runs QEMU 10.0.11, while a range created today pulls a build running QEMU
11.1.0. Booting a captured disk on the newer one changed the virtual hardware
enough that Windows went into Automatic Repair -- observed live on the first
clone deployed from a captured image.

Recording the runtime's digest at capture time lets the deploy pin the exact
image the disk was built against, which is also what ADR-0007 asks for
(everything referenced by digest, no drifting tags at runtime).

Nullable: images captured before this column exists have no recorded runtime
and keep the previous behaviour of resolving the tag.
"""

import sqlalchemy as sa
from alembic import op

revision = "a7c1d2e3f4b5"
down_revision = "99b0004c8d8e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "golden_images",
        sa.Column("runtime_image_digest", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("golden_images", "runtime_image_digest")
