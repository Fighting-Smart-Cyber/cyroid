# backend/alembic/versions/d5e6f7a8b9c0_add_vm_devices.py
"""Add devices/applied_devices columns to VMs

Lets a VM opt into a host device (currently /dev/net/tun, /dev/kvm, /dev/fuse)
without editing the catalog image. Stored as a list of allow-list KEYS, never
raw host paths - the key is resolved server-side, so a client cannot map an
arbitrary device such as a host block device into a student-reachable container.

Mirrors environment/applied_environment: devices are fixed at container-create
time, so the desired list and what the live container actually got diverge until
POST /vms/{id}/apply-config recreates it. No backfill - NULL means "no extra
devices requested", which is the correct default for every existing VM, and the
image's own container_config devices are merged in separately at create time.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-08-19 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vms", sa.Column("devices", sa.JSON(), nullable=True))
    op.add_column("vms", sa.Column("applied_devices", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("vms", "applied_devices")
    op.drop_column("vms", "devices")
