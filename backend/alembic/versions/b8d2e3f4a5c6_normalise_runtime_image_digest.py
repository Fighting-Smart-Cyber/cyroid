"""normalise golden_images.runtime_image_digest to repo@sha256

Revision ID: b8d2e3f4a5c6
Revises: a7c1d2e3f4b5
Create Date: 2026-08-28 14:40:00.000000

The digest was stored exactly as the RepoDigest came back from the container,
which is qualified with whatever registry the image was pulled from -- here the
local mirror, 172.30.0.16:5000. That is correct for this host and meaningless
on any other, and it also broke two things on this one: the warm pool matches
on an image-set label written from tags, so a mirror-qualified digest never
matched and every Windows range cold-started; and the host daemon cannot pull
it (plain-HTTP mirror, HTTPS client), so the pull fell back into DinD and the
cache-back failed on every deploy.

Strips any leading registry host from existing rows. The registry is applied
when pulling instead, which is where it belongs.
"""

import re

import sqlalchemy as sa
from alembic import op

revision = "b8d2e3f4a5c6"
down_revision = "a7c1d2e3f4b5"
branch_labels = None
depends_on = None

# A first segment is a registry host if it has a dot or a colon; "dockurr" has
# neither, so a bare repo is left alone.
_REGISTRY_PREFIX = re.compile(r"^(?:[^/]*[.:][^/]*|localhost)/")


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, runtime_image_digest FROM golden_images "
            "WHERE runtime_image_digest IS NOT NULL"
        )
    ).fetchall()
    for row_id, digest in rows:
        stripped = _REGISTRY_PREFIX.sub("", digest or "", count=1)
        if stripped != digest:
            conn.execute(
                sa.text("UPDATE golden_images SET runtime_image_digest = :d WHERE id = :i"),
                {"d": stripped, "i": row_id},
            )


def downgrade() -> None:
    # The registry that was stripped is not recoverable, and re-qualifying with
    # today's mirror would invent history. Leaving the values normalised is
    # correct on any deployment, so this is deliberately a no-op.
    pass
