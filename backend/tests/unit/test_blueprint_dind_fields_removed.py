"""The DinD-era subnet-allocation fields are gone and stay gone -- COSMOS PG-122 AC2.

`base_subnet_prefix` and `next_offset` allocated a subnet per blueprint instance. DinD isolation
made the allocation unnecessary, both were marked deprecated, and they then survived six months of
"kept for backward compatibility" while three call sites still read and incremented them.

They were not inert while they lasted: `create_range_from_blueprint` took the offset and ignored
it, `_recreate_range_contents` applied it. An instance therefore got the blueprint's addresses when
it was created and shifted addresses the first time it was redeployed. `test_redeploy_keeps_the_
blueprints_own_addresses` is the one that would have caught that, and it is the reason this file
asserts behaviour and not just absence.

What must keep working is the other half of PG-122: a blueprint or an export written before the
removal still loads, because `cyroid-catalog` resolves against this repository and old Blueprint v4
exports are how a defect travels here from a training host.
"""

from __future__ import annotations

import ast
import pathlib
from uuid import uuid4

import pytest

from proving_ground.capability.blueprint import read_blueprint
from proving_ground.models.blueprint import RangeBlueprint, RangeInstance
from proving_ground.schemas.blueprint import BlueprintCreate, BlueprintResponse
from proving_ground.schemas.blueprint_export import BlueprintExportData
from proving_ground.services.blueprint_service import next_instance_ordinal

REMOVED = ("base_subnet_prefix", "next_offset")
PRODUCT = pathlib.Path(__file__).resolve().parents[2] / "proving_ground"


def _identifier_uses(path: pathlib.Path) -> set[str]:
    """Every removed name used as an identifier in this file -- attribute, keyword or assignment.

    Parsed rather than grepped so the two comments that explain why the fields went away do not
    read as uses of them. A guard that cannot survive its own rationale being written down gets
    deleted the first time someone documents the decision.
    """
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.keyword):
            name = node.arg
        elif isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.arg):
            name = node.arg
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A string literal counts: the catalog read these out of blueprint.yaml by name.
            name = node.value
        if name in REMOVED:
            found.add(name)
    return found


def test_no_product_code_uses_the_removed_fields():
    offenders = {}
    for path in sorted(PRODUCT.rglob("*.py")):
        used = _identifier_uses(path)
        if used:
            offenders[str(path.relative_to(PRODUCT.parent))] = sorted(used)
    assert offenders == {}, (
        "PG-122 AC2 removed these fields; something reintroduced them. A blueprint does not "
        f"allocate subnets any more -- every range gets the config's own addresses: {offenders}"
    )


@pytest.mark.parametrize("field", REMOVED)
def test_the_column_is_gone(field):
    assert field not in RangeBlueprint.__table__.columns


@pytest.mark.parametrize("model", [BlueprintCreate, BlueprintResponse, BlueprintExportData])
@pytest.mark.parametrize("field", REMOVED)
def test_the_api_field_is_gone(model, field):
    assert field not in model.model_fields


def test_an_old_export_still_loads_with_the_fields_present():
    """AC3/AC4: a v4 export written before the removal still imports.

    Pydantic ignores keys a model does not declare, which is what makes the removal safe -- but
    that is a default, and a `model_config` with `extra="forbid"` added to this schema later would
    turn every export on every training host into a failed import. This is the tripwire for that.
    """
    old = BlueprintExportData.model_validate(
        {
            "name": "Convoy Ambush",
            "description": "written before PG-122",
            "version": 4,
            "base_subnet_prefix": "10.0.0.0/8",
            "next_offset": 7,
            "config": {"networks": [], "vms": []},
        }
    )
    assert old.name == "Convoy Ambush"
    assert old.version == 4
    assert not hasattr(old, "base_subnet_prefix")


def test_an_old_blueprint_config_still_reads_as_v1():
    """The fields also appear inside `config` on catalog blueprints, where they are just JSON."""
    spec = read_blueprint(
        {"base_subnet_prefix": "10.0.0.0/8", "next_offset": 3, "networks": [], "vms": []}
    )
    assert spec.is_legacy
    assert spec.deployable_on_kubernetes is False


def test_next_instance_ordinal_counts_from_the_instance_table(db_session):
    """The UI numbers instances with this, so it has to advance -- from the table, not a counter."""
    blueprint = RangeBlueprint(name="bp", config={}, version=1)
    db_session.add(blueprint)
    db_session.flush()

    assert next_instance_ordinal(db_session, blueprint.id) == 0

    other = RangeBlueprint(name="other", config={}, version=1)
    db_session.add(other)
    db_session.flush()

    instructor = uuid4()
    for expected in range(3):
        assert next_instance_ordinal(db_session, blueprint.id) == expected
        db_session.add(
            RangeInstance(
                name=f"i{expected}",
                blueprint_id=blueprint.id,
                blueprint_version=1,
                subnet_offset=next_instance_ordinal(db_session, blueprint.id),
                instructor_id=instructor,
                range_id=uuid4(),
            )
        )
        db_session.flush()

    assert next_instance_ordinal(db_session, blueprint.id) == 3
    # Counted per blueprint, not globally: another blueprint's instances are not this one's.
    assert next_instance_ordinal(db_session, other.id) == 0


def test_redeploy_keeps_the_blueprints_own_addresses(db_session):
    """The divergence PG-122 closed: create ignored the offset, redeploy applied it.

    Asserted against the config's own addresses rather than against a remembered pair of values,
    so it fails if a renumbering step is reintroduced under any prefix.
    """
    from proving_ground.api.instances import _recreate_range_contents
    from proving_ground.models import Network, Range, RangeStatus
    from proving_ground.schemas.blueprint import BlueprintConfig

    config = BlueprintConfig.model_validate(
        {
            "networks": [
                {"name": "dmz", "subnet": "10.0.10.0/24", "gateway": "10.0.10.1"},
                {"name": "internal", "subnet": "10.0.20.0/24", "gateway": "10.0.20.1"},
            ],
            "vms": [],
        }
    )
    range_obj = Range(name="r", created_by=uuid4(), status=RangeStatus.DRAFT)
    db_session.add(range_obj)
    db_session.flush()

    _recreate_range_contents(db_session, range_obj, config)
    db_session.flush()

    got = {
        n.name: (n.subnet, n.gateway)
        for n in db_session.query(Network).filter(Network.range_id == range_obj.id)
    }
    assert got == {
        "dmz": ("10.0.10.0/24", "10.0.10.1"),
        "internal": ("10.0.20.0/24", "10.0.20.1"),
    }
