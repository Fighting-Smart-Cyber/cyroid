# backend/tests/unit/test_catalog_contribution_api.py
"""The contribute-back endpoints, end to end (PG-147).

The route functions are called directly rather than over TestClient: the app's
startup hooks want Redis, and nothing here needs them. Everything below the
route is real -- a catalog on disk, the real installer, the real service and
the real diff -- so the diff is computed over exactly what an install
produces rather than over a hand-built config.
"""

import json
from uuid import uuid4

import pytest
import yaml
from fastapi import HTTPException

from proving_ground.api.blueprints import (
    build_blueprint_catalog_contribution,
    get_blueprint_catalog_diff,
)
from proving_ground.catalog.contribution import render_yaml
from proving_ground.models.blueprint import RangeBlueprint
from proving_ground.models.catalog import CatalogSource, CatalogSourceType
from proving_ground.schemas.catalog_contribution import BlueprintContributionRequest
from proving_ground.services.catalog_service import CatalogService


def k(*segments: str) -> str:
    """Stable change key matching FieldChange.key (JSON array of segments)."""
    return json.dumps(list(segments), ensure_ascii=False, separators=(",", ":"))


BLUEPRINT_DOC = {
    "name": "Red Team Training Lab",
    "description": "A small lab.",
    "seed_id": "pg147-red-team-lab",
    "networks": [{"name": "corp", "subnet": "10.10.0.0/24", "gateway": "10.10.0.1"}],
    "vms": [
        {
            "hostname": "dc-01",
            "base_image_tag": "windows-2019",
            "cpu": 4,
            "ram_mb": 8192,
            "network_name": "corp",
            "ip_address": "10.10.0.10",
        }
    ],
}

USER_ID = uuid4()


def write_catalog(root, *, item_path="blueprints/red-team", doc=None):
    """A minimal but real local catalog: index.json plus one blueprint."""
    item_dir = root / item_path
    item_dir.mkdir(parents=True, exist_ok=True)
    (item_dir / "blueprint.yaml").write_text(render_yaml(doc or BLUEPRINT_DOC), encoding="utf-8")
    (root / "index.json").write_text(
        json.dumps(
            {
                "catalog": {"name": "Test Catalog", "version": "1.0"},
                "items": [
                    {
                        "id": "red-team-training-lab",
                        "type": "blueprint",
                        "name": "Red Team Training Lab",
                        "description": "A small lab.",
                        "path": item_path,
                        "version": "1.0.0",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def catalog(db_session, tmp_path):
    """A local catalog source, synced and ready to install from."""
    root = write_catalog(tmp_path / "catalog")
    source = CatalogSource(name="Test Catalog", source_type=CatalogSourceType.LOCAL, url=str(root))
    db_session.add(source)
    db_session.commit()
    return root, source


@pytest.fixture
def installed_blueprint(db_session, catalog):
    """Install the catalog blueprint through the real installer."""
    _, source = catalog
    installed = CatalogService(db_session).install_item(
        source, "red-team-training-lab", USER_ID, build_images=False
    )
    db_session.commit()
    return installed.local_resource_id


def diff_of(db_session, blueprint_id):
    return get_blueprint_catalog_diff(blueprint_id, db_session, None)


def contribute(db_session, blueprint_id, changes):
    return build_blueprint_catalog_contribution(
        blueprint_id, BlueprintContributionRequest(changes=changes), db_session, None
    )


def edit(db_session, blueprint_id, mutate):
    """Apply a local edit the way the range builder would."""
    blueprint = db_session.query(RangeBlueprint).filter(RangeBlueprint.id == blueprint_id).first()
    config = json.loads(json.dumps(blueprint.config))  # detach from the JSON column
    mutate(config)
    blueprint.config = config
    db_session.commit()


def test_freshly_installed_blueprint_reports_no_changes(db_session, installed_blueprint):
    result = diff_of(db_session, installed_blueprint)
    assert result.has_changes is False
    assert result.changes == []
    assert result.origin.item_path == "blueprints/red-team/blueprint.yaml"
    assert result.origin.item_id == "red-team-training-lab"
    assert result.origin.source_name == "Test Catalog"


def test_local_edit_shows_up_as_a_named_field_change(db_session, installed_blueprint):
    edit(db_session, installed_blueprint, lambda c: c["vms"][0].update(cpu=8))

    result = diff_of(db_session, installed_blueprint)
    assert result.has_changes is True
    assert len(result.changes) == 1
    change = result.changes[0]
    assert change.key == k("vms", "dc-01", "cpu")
    assert change.path == ["vms", "dc-01", "cpu"]
    assert (change.before, change.after) == (4, 8)
    assert change.label == "VM 'dc-01' cpu"


def test_contribution_returns_an_applicable_patch(db_session, installed_blueprint):
    def bump(config):
        config["vms"][0]["cpu"] = 8
        config["vms"][0]["ram_mb"] = 16384

    edit(db_session, installed_blueprint, bump)
    result = contribute(db_session, installed_blueprint, [k("vms", "dc-01", "cpu")])

    assert result.applied == [k("vms", "dc-01", "cpu")]
    assert result.applies_to_source is True
    assert "-  cpu: 4" in result.patch and "+  cpu: 8" in result.patch
    assert "blueprints/red-team/blueprint.yaml" in result.patch
    assert result.suggested_filename == "red-team-training-lab-contribution.patch"

    # The unselected change must not leak into the contribution.
    updated = yaml.safe_load(result.blueprint_yaml)
    assert updated["vms"][0]["cpu"] == 8
    assert updated["vms"][0]["ram_mb"] == 8192
    assert updated["seed_id"] == "pg147-red-team-lab"


def test_omitting_the_selection_contributes_everything(db_session, installed_blueprint):
    def bump(config):
        config["vms"][0]["cpu"] = 8
        config["networks"][0]["internet_enabled"] = True

    edit(db_session, installed_blueprint, bump)
    result = contribute(db_session, installed_blueprint, None)
    assert sorted(result.applied) == [
        k("networks", "corp", "internet_enabled"),
        k("vms", "dc-01", "cpu"),
    ]


def test_selecting_a_stale_change_is_refused(db_session, installed_blueprint):
    edit(db_session, installed_blueprint, lambda c: c["vms"][0].update(cpu=8))

    with pytest.raises(HTTPException) as exc:
        contribute(db_session, installed_blueprint, [k("vms", "dc-01", "ram_mb")])
    assert exc.value.status_code == 400
    assert k("vms", "dc-01", "ram_mb") in exc.value.detail


def test_contributing_nothing_is_refused(db_session, installed_blueprint):
    with pytest.raises(HTTPException) as exc:
        contribute(db_session, installed_blueprint, [])
    assert exc.value.status_code == 400
    assert "nothing to contribute" in exc.value.detail


def test_a_locally_authored_blueprint_has_no_catalog_to_contribute_to(db_session):
    blueprint = RangeBlueprint(
        name="Mine", description="local", config={"networks": [], "vms": []}, version=1
    )
    db_session.add(blueprint)
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        diff_of(db_session, blueprint.id)
    assert exc.value.status_code == 404
    assert "not installed from a catalog" in exc.value.detail


def test_an_unknown_blueprint_is_a_404(db_session):
    with pytest.raises(HTTPException) as exc:
        diff_of(db_session, uuid4())
    assert exc.value.status_code == 404
    assert exc.value.detail == "Blueprint not found"


def test_a_catalog_path_escaping_the_root_is_refused(db_session, catalog, installed_blueprint):
    """index.json is catalog-supplied, so its paths are not trusted."""
    root, _ = catalog
    index = json.loads((root / "index.json").read_text())
    index["items"][0]["path"] = "../../../../etc"
    (root / "index.json").write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(HTTPException) as exc:
        diff_of(db_session, installed_blueprint)
    assert exc.value.status_code == 409
    assert "outside the catalog root" in exc.value.detail


def test_a_catalog_whose_blueprint_file_vanished_is_a_409(db_session, catalog, installed_blueprint):
    root, _ = catalog
    (root / "blueprints/red-team/blueprint.yaml").unlink()

    with pytest.raises(HTTPException) as exc:
        diff_of(db_session, installed_blueprint)
    assert exc.value.status_code == 409
    assert "missing" in exc.value.detail


def test_index_path_with_dotdot_exports_sandbox_relative_item_path(
    db_session, catalog, installed_blueprint
):
    """Raw index path may contain `..` but item_path must be the resolved relative."""
    root, _ = catalog
    # Still resolves inside the catalog root to the same blueprint.yaml.
    index = json.loads((root / "index.json").read_text())
    index["items"][0]["path"] = "blueprints/public/../../blueprints/red-team"
    (root / "index.json").write_text(json.dumps(index), encoding="utf-8")

    result = diff_of(db_session, installed_blueprint)
    assert result.origin.item_path == "blueprints/red-team/blueprint.yaml"
    assert ".." not in result.origin.item_path
    assert not result.origin.item_path.startswith("/")


def test_a_non_string_catalog_path_is_refused(db_session, catalog, installed_blueprint):
    """JSON null / non-string path must be a 409, not an unhandled 500."""
    root, _ = catalog
    index = json.loads((root / "index.json").read_text())
    index["items"][0]["path"] = None
    (root / "index.json").write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(HTTPException) as exc:
        diff_of(db_session, installed_blueprint)
    assert exc.value.status_code == 409
    assert "non-string" in exc.value.detail


def test_missing_blueprint_detail_does_not_leak_absolute_paths(
    db_session, catalog, installed_blueprint
):
    root, _ = catalog
    (root / "blueprints/red-team/blueprint.yaml").unlink()

    with pytest.raises(HTTPException) as exc:
        diff_of(db_session, installed_blueprint)
    assert exc.value.status_code == 409
    assert "missing" in exc.value.detail
    # Should-fix: 409 detail must not expose absolute server paths.
    assert str(root) not in exc.value.detail
    assert "blueprints/red-team/blueprint.yaml" in exc.value.detail
