# backend/tests/unit/test_catalog_installer.py
"""One-click install with automatic dependency resolution (PG-149).

Driven against a real catalog on disk through the real CatalogService, so the
plan is executed against what an actual install does rather than against mocks.
Docker is never invoked: ``build_images=False`` registers the image project
without building it.
"""

import json
from uuid import uuid4

import pytest

from proving_ground.catalog.dependencies import DependencyInstallError
from proving_ground.catalog.installer import CatalogInstaller
from proving_ground.models.base_image import BaseImage
from proving_ground.models.blueprint import RangeBlueprint
from proving_ground.models.catalog import CatalogInstalledItem, CatalogSource, CatalogSourceType
from proving_ground.services import catalog_service as catalog_service_module
from proving_ground.services.catalog_service import CatalogService

USER_ID = uuid4()

BLUEPRINT_YAML = """\
name: Red Team Lab
description: A small lab.
seed_id: pg149-red-team-lab
networks:
  - name: corp
    subnet: 10.10.0.0/24
    gateway: 10.10.0.1
vms:
  - hostname: dc-01
    base_image_tag: ubuntu-22-04
    network_name: corp
    ip_address: 10.10.0.10
"""

BASE_IMAGE_YAML = """\
name: Ubuntu 22.04
description: Ubuntu base
vm_type: container
os_type: linux
base_image: docker.io/library/ubuntu:22.04
"""


def build_catalog(
    root,
    *,
    requires_base=("ubuntu-22-04",),
    requires_images=("custom-web",),
    with_base_image_file=True,
    with_dockerfile=True,
    walkthrough=False,
):
    """A real catalog tree: index.json, a blueprint, a base image and an image."""
    (root / "blueprints/red-team-lab").mkdir(parents=True, exist_ok=True)
    body = BLUEPRINT_YAML
    if walkthrough:
        body += "walkthrough:\n  title: Lab Guide\n  steps: []\n"
    (root / "blueprints/red-team-lab/blueprint.yaml").write_text(body, encoding="utf-8")

    items = [
        {
            "id": "red-team-lab",
            "type": "blueprint",
            "name": "Red Team Lab",
            "description": "A small lab.",
            "path": "blueprints/red-team-lab",
            "version": "1.0.0",
            "requires_base_images": list(requires_base),
            "requires_images": list(requires_images),
        }
    ]

    if with_base_image_file:
        (root / "base_images").mkdir(parents=True, exist_ok=True)
        (root / "base_images/ubuntu-22-04.yaml").write_text(BASE_IMAGE_YAML, encoding="utf-8")
        items.append(
            {
                "id": "ubuntu-22-04",
                "type": "base_image",
                "name": "Ubuntu 22.04",
                "description": "Ubuntu base",
                "path": "base_images/ubuntu-22-04.yaml",
                "version": "1.0.0",
            }
        )

    if with_dockerfile:
        (root / "images/custom-web").mkdir(parents=True, exist_ok=True)
        (root / "images/custom-web/Dockerfile").write_text("FROM nginx:alpine\n", encoding="utf-8")

    (root / "index.json").write_text(
        json.dumps({"catalog": {"name": "Test Catalog", "version": "1.0"}, "items": items}),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def images_dir(tmp_path, monkeypatch):
    """Keep image-project copies inside the test's tmp dir, not /data/images."""
    target = tmp_path / "data-images"
    monkeypatch.setattr(catalog_service_module, "IMAGES_DIR", str(target))
    return target


@pytest.fixture
def catalog(db_session, tmp_path):
    root = build_catalog(tmp_path / "catalog")
    source = CatalogSource(name="Test Catalog", source_type=CatalogSourceType.LOCAL, url=str(root))
    db_session.add(source)
    db_session.commit()
    return root, source


@pytest.fixture
def installer(db_session):
    """An installer that records every step it reports."""
    reported = []

    def progress(step, done, total):
        reported.append((step.key, step.label, done, total))

    inst = CatalogInstaller(CatalogService(db_session), progress=progress)
    inst.reported = reported
    return inst


def test_plan_lists_every_dependency_in_order(installer, catalog):
    _, source = catalog
    plan = installer.build_plan(source, "red-team-lab")
    assert [s.key for s in plan.steps] == [
        "base_image/ubuntu-22-04",
        "image/custom-web",
        "blueprint/red-team-lab",
    ]
    assert plan.total_steps == 3
    assert plan.warnings == []


def test_one_click_install_installs_the_blueprint_and_its_dependencies(
    installer, catalog, db_session, images_dir
):
    """AC 1 and 3: base images and Dockerfile projects install automatically."""
    _, source = catalog
    outcome = installer.install(source, "red-team-lab", USER_ID, build_images=False)

    assert outcome.installed == [
        "base_image/ubuntu-22-04",
        "image/custom-web",
        "blueprint/red-team-lab",
    ]
    assert outcome.missing == [] and outcome.warnings == []

    blueprint = (
        db_session.query(RangeBlueprint).filter(RangeBlueprint.id == outcome.blueprint_id).first()
    )
    assert blueprint is not None and blueprint.name == "Red Team Lab"

    # The base image is registered and recorded as installed from the catalog.
    assert db_session.query(BaseImage).filter(BaseImage.name == "Ubuntu 22.04").first()
    installed_ids = {row.catalog_item_id for row in db_session.query(CatalogInstalledItem).all()}
    assert installed_ids == {"ubuntu-22-04", "red-team-lab"}

    # The image project was copied out of the catalog.
    assert (images_dir / "custom-web" / "Dockerfile").exists()


def test_progress_names_each_stage_as_it_runs(installer, catalog, images_dir):
    """AC 4: the log says what is happening, with a known total up front."""
    _, source = catalog
    installer.install(source, "red-team-lab", USER_ID, build_images=False)

    assert installer.reported == [
        ("base_image/ubuntu-22-04", "Installing VM image: Ubuntu 22.04", 0, 3),
        ("image/custom-web", "Building image: custom-web", 1, 3),
        ("blueprint/red-team-lab", "Installing blueprint: Red Team Lab", 2, 3),
    ]


def test_installing_twice_skips_everything_already_there(installer, catalog, images_dir):
    """AC 8: idempotent."""
    _, source = catalog
    installer.install(source, "red-team-lab", USER_ID, build_images=False)
    installer.reported.clear()

    again = installer.install(source, "red-team-lab", USER_ID, build_images=False)

    assert again.installed == []
    assert sorted(again.skipped) == [
        "base_image/ubuntu-22-04",
        "blueprint/red-team-lab",
        "image/custom-web",
    ]
    assert installer.reported == []


def test_a_base_image_missing_from_the_catalog_is_reported_and_not_attempted(
    db_session, installer, tmp_path, images_dir
):
    """AC 7: the old code logged a warning and installed a broken blueprint."""
    root = build_catalog(tmp_path / "c2", with_base_image_file=False)
    source = CatalogSource(name="C2", source_type=CatalogSourceType.LOCAL, url=str(root))
    db_session.add(source)
    db_session.commit()

    outcome = installer.install(source, "red-team-lab", USER_ID, build_images=False)

    assert outcome.missing == ["base_image/ubuntu-22-04"]
    assert any("not in the catalog index" in w for w in outcome.warnings)
    assert outcome.installed == ["image/custom-web", "blueprint/red-team-lab"]


def test_a_failing_step_says_which_dependency_failed(installer, catalog, images_dir):
    """AC 7: the error names the step, not a frame three levels down."""
    _, source = catalog

    def explode(*args, **kwargs):
        raise RuntimeError("docker build exited 1")

    installer.service._install_image_from_path = explode

    with pytest.raises(DependencyInstallError) as exc:
        installer.install(source, "red-team-lab", USER_ID, build_images=False)

    assert exc.value.dependency.key == "image/custom-web"
    assert "Building image: custom-web" in str(exc.value)
    assert "docker build exited 1" in str(exc.value)


def test_a_failed_step_leaves_the_steps_before_it_installed(
    installer, catalog, db_session, images_dir
):
    """Not a transaction: an installed base image is still a usable base image."""
    _, source = catalog
    installer.service._install_image_from_path = lambda *a, **k: 1 / 0

    with pytest.raises(DependencyInstallError):
        installer.install(source, "red-team-lab", USER_ID, build_images=False)

    assert db_session.query(BaseImage).filter(BaseImage.name == "Ubuntu 22.04").first()
    assert db_session.query(RangeBlueprint).count() == 0


def test_cancelling_stops_before_the_next_step(installer, catalog, images_dir):
    _, source = catalog
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 1  # allow the first step, stop before the second

    outcome = installer.install(
        source, "red-team-lab", USER_ID, build_images=False, is_cancelled=cancelled
    )

    assert outcome.cancelled is True
    assert outcome.installed == ["base_image/ubuntu-22-04"]
    assert outcome.blueprint_id is None


def test_content_shipped_with_a_blueprint_is_installed_and_verified(
    db_session, installer, tmp_path, images_dir
):
    """AC 2: referenced Content items come with the blueprint."""
    root = build_catalog(tmp_path / "c3", requires_base=(), requires_images=(), walkthrough=True)
    source = CatalogSource(name="C3", source_type=CatalogSourceType.LOCAL, url=str(root))
    db_session.add(source)
    db_session.commit()

    plan = installer.build_plan(source, "red-team-lab")
    assert [s.kind for s in plan.steps] == ["blueprint", "content"]

    outcome = installer.install(source, "red-team-lab", USER_ID, build_images=False)
    assert outcome.content_ids
    blueprint = (
        db_session.query(RangeBlueprint).filter(RangeBlueprint.id == outcome.blueprint_id).first()
    )
    assert blueprint.content_ids == outcome.content_ids


def test_planning_a_non_blueprint_item_is_refused(installer, catalog):
    _, source = catalog
    with pytest.raises(ValueError, match="only blueprints"):
        installer.build_plan(source, "ubuntu-22-04")


def test_planning_an_unknown_item_is_refused(installer, catalog):
    _, source = catalog
    with pytest.raises(ValueError, match="not found"):
        installer.build_plan(source, "nope")
