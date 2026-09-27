"""range.visibility must round-trip its enum VALUE, not its NAME.

Reported live: a user deployed a range and could not see it. The suspicion was
the visibility filter, but that was correct -- `created_by == current_user.id`
is its first condition, so an owner always sees their own range.

The real defect was one column declaration:

    visibility: Mapped[RangeVisibility] = mapped_column(default=...)

SQLAlchemy infers an Enum type from the annotation and persists member NAMES
("PRIVATE"). The migration that added the column declared it String(20) with
server_default="private" -- the VALUE. So the column held both spellings, and
hydrating a lowercase row raised

    LookupError: 'private' is not among the defined enum values

That failed the entire GET /ranges listing rather than the single bad row, so
the symptom was "I can see none of my ranges", which looks exactly like an
authorization bug and is not one.

These tests exercise the configured type rather than reading the source, so
reverting the fix fails them.
"""

import pytest
from sqlalchemy.dialects import postgresql

from proving_ground.models.range import Range, RangeVisibility

DIALECT = postgresql.dialect()
COLUMN = Range.__table__.c.visibility


def _to_db(member):
    """What the ORM would write for this member."""
    proc = COLUMN.type.bind_processor(DIALECT)
    return proc(member) if proc else member


def _from_db(stored):
    """What the ORM reads back for this stored string."""
    proc = COLUMN.type.result_processor(DIALECT, None)
    return proc(stored) if proc else stored


def _server_default():
    """The literal the migration writes. Plain str today; tolerate text()."""
    arg = COLUMN.server_default.arg
    return getattr(arg, "text", arg).strip("'")


class TestStorageIsTheValue:
    def test_the_column_knows_the_values_not_the_names(self):
        # Names would be PRIVATE / SHARED / PUBLIC.
        assert sorted(COLUMN.type.enums) == ["private", "public", "shared"]

    @pytest.mark.parametrize("member", list(RangeVisibility))
    def test_each_member_writes_its_lowercase_value(self, member):
        assert _to_db(member) == member.value
        assert _to_db(member).islower()

    @pytest.mark.parametrize("member", list(RangeVisibility))
    def test_each_value_reads_back_as_its_member(self, member):
        assert _from_db(member.value) is member


class TestTheServerDefaultIsReadable:
    """The exact failure. The migration's server_default writes a literal
    string; the ORM has to be able to read it back."""

    def test_the_server_default_matches_a_real_enum_value(self):
        default = _server_default()
        assert default in {m.value for m in RangeVisibility}

    def test_a_row_written_by_the_server_default_hydrates(self):
        default = _server_default()
        assert _from_db(default) is RangeVisibility.PRIVATE

    def test_the_old_name_spelling_is_not_silently_accepted(self):
        """If both spellings read, the column has drifted back to ambiguity and
        a mixed-case table would go unnoticed again."""
        with pytest.raises(LookupError):
            _from_db("PRIVATE")


class TestTheWireFormatAgrees:
    def test_values_are_lowercase_for_the_frontend_union(self):
        """frontend/src/services/api.ts declares
        `type RangeVisibility = 'private' | 'shared' | 'public'`."""
        assert {m.value for m in RangeVisibility} == {"private", "shared", "public"}

    def test_str_enum_so_pydantic_serialises_the_value(self):
        assert isinstance(RangeVisibility.PRIVATE, str)
        assert RangeVisibility.PRIVATE == "private"
