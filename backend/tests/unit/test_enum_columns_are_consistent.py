"""An enum-typed column must store one spelling, decided deliberately.

`ranges.visibility` was declared `Mapped[RangeVisibility]` while its migration
created a bare `String(20)` with `server_default="private"`. SQLAlchemy's
inferred Enum persists member NAMES ("PRIVATE"); the server_default wrote the
VALUE ("private"); nothing rejected either, because an unconstrained varchar
accepts both. Reading a row written by the server_default then raised

    LookupError: 'private' is not among the defined enum values

and one unreadable row failed the entire GET /ranges listing.

Auditing the database afterwards, 17 of 19 enum columns are native PostgreSQL
enum types, which cannot develop this problem -- Postgres rejects any label it
does not know. `ranges.visibility` was the only enum-backed varchar, and it is
now pinned with values_callable. (`event_participants.role` is also varchar but
is a plain Mapped[str], so there is no name/value split to get wrong.)

This guards the shape rather than the instance: a future column declared as an
enum but given a bare string type would reintroduce exactly the same silent
divergence.
"""

import inspect
import pkgutil
import re

import proving_ground.models as models_pkg

# `mapped_column(` up to its matching close, for a Mapped[<Something>] attribute
# whose type is not a builtin.
_ENUM_COL = re.compile(
    r"^\s*(?P<attr>\w+)\s*:\s*Mapped\[(?P<type>[A-Z]\w*)\]\s*=\s*mapped_column\((?P<args>.*?)\)\s*$",
    re.M | re.S,
)
_BUILTINS = {"UUID", "JSON", "Optional", "List", "Dict", "Any"}


def _model_sources():
    for m in pkgutil.iter_modules(models_pkg.__path__):
        mod = __import__(f"proving_ground.models.{m.name}", fromlist=["x"])
        yield m.name, inspect.getsource(mod)


def _enum_columns():
    """(module, attribute, declared type, mapped_column args)."""
    out = []
    for name, src in _model_sources():
        # Only types defined as Enums in that same module.
        enums = set(re.findall(r"^class (\w+)\((?:str,\s*)?Enum\):", src, re.M))
        for m in _ENUM_COL.finditer(src):
            t = m.group("type")
            if t in enums and t not in _BUILTINS:
                out.append((name, m.group("attr"), t, m.group("args")))
    return out


class TestTheAuditFoundSomething:
    def test_there_are_enum_columns_to_check(self):
        """If the parser finds nothing, everything below passes vacuously."""
        cols = _enum_columns()
        assert len(cols) >= 5, f"parsed only {len(cols)} enum columns; the guard is not working"


class TestNoEnumColumnIsAnUnconstrainedString:
    def test_none_declares_a_bare_string_type(self):
        """`mapped_column(String(20), default=SomeEnum.X)` is the exact shape
        that broke visibility: SQLAlchemy writes NAMES, anything writing the
        column directly writes VALUES, and varchar accepts both."""
        bad = [
            f"{mod}.{attr}: Mapped[{typ}] with a bare String()"
            for mod, attr, typ, args in _enum_columns()
            if re.search(r"\bString\s*\(", args) and "values_callable" not in args
        ]
        assert not bad, (
            "These columns are enum-typed but stored as an unconstrained "
            "string, so two spellings can coexist:\n  " + "\n  ".join(bad)
        )

    def test_a_column_that_pins_values_says_so_explicitly(self):
        """Storing values instead of names is a fine choice; it just has to be
        stated, so the migration and the model cannot drift apart in silence."""
        for mod, attr, _typ, args in _enum_columns():
            if "values_callable" in args:
                assert "SAEnum" in args or "Enum(" in args, (
                    f"{mod}.{attr} uses values_callable without an explicit Enum "
                    "type; the two must be declared together"
                )
