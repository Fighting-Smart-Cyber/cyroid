"""add platform_secrets

Revision ID: c9e4f5a6b7d8
Revises: b8d2e3f4a5c6
Create Date: 2026-08-29 13:45:00.000000

Somewhere for the platform to hold secrets about itself. The first is the
credential the in-UI update uses to fetch from the code remote.

Not the environment: an operator with a browser and no shell cannot edit a
.env, and Compose loads .env into every container, so a token there is
readable from any service that happens to be compromised rather than only the
one that needs it.

Values are Fernet ciphertext, keyed from the JWT secret -- the same treatment
export_service already gives stored passwords.
"""

import sqlalchemy as sa
from alembic import op

revision = "c9e4f5a6b7d8"
down_revision = "b8d2e3f4a5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_secrets",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column("public_part", sa.String(length=255), nullable=True),
        sa.Column(
            "updated_by_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_platform_secrets_key", "platform_secrets", ["key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_platform_secrets_key", table_name="platform_secrets")
    op.drop_table("platform_secrets")
