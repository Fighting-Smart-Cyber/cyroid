"""Guards against SQLAlchemy models drifting from the migrated database schema.

When a model omits something a migration created - an ondelete rule, an index -
`alembic revision --autogenerate` proposes to remove it from the database on
every single run. Anyone following the standard migrate workflow then generates
a migration that silently strips it. These assertions pin the model to what the
migrations actually built.

Metadata only: no database connection, so this runs anywhere the suite does.
"""

from proving_ground.models.base_image import BaseImage
from proving_ground.models.content import Content
from proving_ground.models.golden_image import GoldenImage


def _fk(model, column_name):
    for fk in model.__table__.foreign_keys:
        if fk.parent.name == column_name:
            return fk
    raise AssertionError(f"{model.__tablename__}.{column_name} has no foreign key")


def test_base_image_created_by_keeps_set_null():
    # Migration built this FK as ON DELETE SET NULL: deleting a user nulls
    # their authorship instead of blocking the delete.
    assert _fk(BaseImage, "created_by").ondelete == "SET NULL"


def test_golden_image_created_by_keeps_set_null():
    assert _fk(GoldenImage, "created_by").ondelete == "SET NULL"


def test_content_source_range_id_is_indexed():
    # ix_content_source_range_id exists in the database; content is looked up
    # by source range during range teardown.
    indexed = {col.name for index in Content.__table__.indexes for col in index.columns}
    assert "source_range_id" in indexed


def test_content_source_range_id_keeps_set_null():
    assert _fk(Content, "source_range_id").ondelete == "SET NULL"
