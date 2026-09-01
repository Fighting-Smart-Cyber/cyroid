# backend/alembic/script.py.mako
"""drop unmodelled vm_templates table

Revision ID: 99b0004c8d8e
Revises: 74b9c29ef5c7
Create Date: 2026-08-27 18:36:34.463286

The vm_templates table has no SQLAlchemy model (there is no VMTemplate class),
so it is absent from Base.metadata and `alembic revision --autogenerate`
proposes dropping it (and the now-pointless vms.template_id FK/column) on
every run. Verified live before writing this migration: vm_templates has 0
rows, and vms.template_id is NULL on all rows / read nowhere in backend code
(grep across backend/proving_ground turned up nothing; the only other
"template_id" hit, schemas/export.py's `existing_template_id`, is an
unrelated blueprint-import field). Frontend TypeScript still carries a
`template_id` field in a couple of places but it is explicitly commented
"Legacy .../ deprecated, use base_image_id instead" and is never populated
from or sent to a backend schema that accepts it, so it is inert.

This migration intentionally contains ONLY the vm_templates/template_id
drop. `--autogenerate` also proposed unrelated destructive noise (dropping
and recreating the created_by foreign keys on base_images and golden_images,
and dropping ix_content_source_range_id on content) caused by pre-existing
drift between those models and the live schema that has nothing to do with
vm_templates. Those lines were deliberately removed from the generated
output rather than applied here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "99b0004c8d8e"
down_revision: Union[str, None] = "74b9c29ef5c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ### vm_templates is unmodelled (no SQLAlchemy class) and has 0 rows;
    # ### vms.template_id is a dead column nothing reads. Drop both. ###
    op.drop_index("ix_vm_templates_name", table_name="vm_templates")
    op.drop_index("ix_vm_templates_os_family", table_name="vm_templates")
    op.drop_constraint("vms_template_id_fkey", "vms", type_="foreignkey")
    op.drop_column("vms", "template_id")
    op.drop_table("vm_templates")


def downgrade() -> None:
    # ### recreate vm_templates and vms.template_id exactly as they were ###
    op.create_table(
        "vm_templates",
        sa.Column("name", sa.VARCHAR(length=100), autoincrement=False, nullable=False),
        sa.Column("description", sa.TEXT(), autoincrement=False, nullable=True),
        sa.Column(
            "os_type",
            sa.Enum("WINDOWS", "LINUX", "CUSTOM", "NETWORK", name="ostype"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("os_variant", sa.VARCHAR(length=100), autoincrement=False, nullable=False),
        sa.Column("base_image", sa.VARCHAR(length=255), autoincrement=False, nullable=False),
        sa.Column("default_cpu", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column("default_ram_mb", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column("default_disk_gb", sa.INTEGER(), autoincrement=False, nullable=False),
        sa.Column("config_script", sa.TEXT(), autoincrement=False, nullable=True),
        sa.Column("tags", sa.JSON(), autoincrement=False, nullable=False),
        sa.Column("created_by", sa.UUID(), autoincrement=False, nullable=True),
        sa.Column("id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("golden_image_path", sa.VARCHAR(length=500), autoincrement=False, nullable=True),
        sa.Column("cached_iso_path", sa.VARCHAR(length=500), autoincrement=False, nullable=True),
        sa.Column(
            "is_cached",
            sa.BOOLEAN(),
            server_default=sa.text("false"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "vm_type",
            sa.Enum("CONTAINER", "LINUX_VM", "WINDOWS_VM", name="vmtype"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("linux_distro", sa.VARCHAR(length=50), autoincrement=False, nullable=True),
        sa.Column("boot_mode", sa.VARCHAR(length=10), autoincrement=False, nullable=True),
        sa.Column("disk_type", sa.VARCHAR(length=10), autoincrement=False, nullable=True),
        sa.Column("iso_url_x86", sa.VARCHAR(length=500), autoincrement=False, nullable=True),
        sa.Column("iso_url_arm64", sa.VARCHAR(length=500), autoincrement=False, nullable=True),
        sa.Column(
            "native_arch",
            sa.VARCHAR(length=20),
            server_default=sa.text("'x86_64'::character varying"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "is_seed",
            sa.BOOLEAN(),
            server_default=sa.text("false"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column("seed_id", sa.VARCHAR(length=100), autoincrement=False, nullable=True),
        sa.Column("os_family", sa.VARCHAR(length=50), autoincrement=False, nullable=True),
        sa.Column("os_version", sa.VARCHAR(length=20), autoincrement=False, nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="vm_templates_created_by_fkey"),
        sa.PrimaryKeyConstraint("id", name="vm_templates_pkey"),
        sa.UniqueConstraint("seed_id", name="uq_vm_templates_seed_id"),
    )
    op.create_index("ix_vm_templates_os_family", "vm_templates", ["os_family"], unique=False)
    op.create_index("ix_vm_templates_name", "vm_templates", ["name"], unique=False)
    op.add_column("vms", sa.Column("template_id", sa.UUID(), autoincrement=False, nullable=True))
    op.create_foreign_key("vms_template_id_fkey", "vms", "vm_templates", ["template_id"], ["id"])
