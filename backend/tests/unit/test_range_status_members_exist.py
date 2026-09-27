"""Every `RangeStatus.X` written in the deployment tasks must be a real member.

This exists because it wasn't. The Era B deploy path set `RangeStatus.DEPLOYED`, which does not
exist -- the enum is DRAFT/DEPLOYING/RUNNING/STOPPED/ARCHIVED/ERROR. Nothing caught it: the unit
tests covered the capability layer, not the task wrapper, and an `AttributeError` on an enum only
fires when that line is actually reached, which is inside a deploy against a real cluster.

A static scan is the right shape of guard here. It costs nothing, it covers every branch including
the error paths that are hardest to reach in a test, and it fails at the typo rather than at the
deploy.
"""

import ast
from pathlib import Path

import pytest

from proving_ground.models.range import RangeStatus

MODULES = [
    Path(__file__).parents[2] / "proving_ground" / "tasks" / "deployment.py",
    Path(__file__).parents[2] / "proving_ground" / "services" / "kubernetes_range_service.py",
]


def referenced_statuses(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "RangeStatus"
    }


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_every_referenced_status_exists(path):
    members = {m.name for m in RangeStatus}
    unknown = referenced_statuses(path) - members
    assert not unknown, f"{path.name} references non-existent RangeStatus: {sorted(unknown)}"


def test_the_scan_would_actually_catch_a_typo(tmp_path):
    # A guard that cannot fail is not a guard.
    bogus = tmp_path / "bogus.py"
    bogus.write_text("range_obj.status = RangeStatus.DEPLOYED\n")
    assert referenced_statuses(bogus) - {m.name for m in RangeStatus} == {"DEPLOYED"}


def test_both_substrates_agree_on_the_terminal_status():
    """Era A and Era B must land a successful range in the same state.

    If they diverge, every consumer of `status` -- the UI badge, the console guard, the pool
    reaper -- grows a per-substrate special case, and MIG-3 stops being a deletion.
    """
    era_a = referenced_statuses(
        Path(__file__).parents[2] / "proving_ground" / "services" / "range_deployment_service.py"
    )
    era_b = referenced_statuses(MODULES[0])
    assert "RUNNING" in era_a and "RUNNING" in era_b
