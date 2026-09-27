# backend/tests/unit/test_catalog_v2.py
"""Installing an Era B catalog item, and refusing one written for the other era -- CAT-047.

Everything the catalog installed used to be a v1 blueprint: the config was assembled from a fixed
set of Era A keys, so an item declaring workloads and capabilities lost them on the way in and the
storefront reported a clean install of something the Kubernetes substrate refuses at deploy.

Driven through the real `CatalogService` against a real catalog on disk, because the bug lived in
the gap between what the catalog file said and what the blueprint row ended up holding, and a mock
of either end cannot show that gap.
"""

import json

import pytest
import yaml

from proving_ground import config as config_module
from proving_ground.capability.blueprint import SCHEMA_VERSION_K8S, read_blueprint
from proving_ground.capability.models import Scope
from proving_ground.catalog.dependencies import (
    BASE_IMAGE,
    IMAGE,
    SUBSTRATE_DOCKER,
    SUBSTRATE_KUBERNETES,
    resolve_install_plan,
)
from proving_ground.config import Settings
from proving_ground.models.blueprint import RangeBlueprint
from proving_ground.models.catalog import CatalogSource, CatalogSourceType
from proving_ground.services import catalog_service as catalog_service_module
from proving_ground.services.catalog_service import (
    CatalogService,
    build_config_from_yaml,
    catalog_item_schema_version,
)

from uuid import uuid4

USER_ID = uuid4()

# The repository the fixture's capability pulls from, and the one an operator has permitted. Kept
# apart so a test can permit neither, one, or something else entirely.
PERMITTED_REPOSITORY = "https://charts.example.test/stable"
CIRROS = (
    "quay.io/kubevirt/cirros-container-disk-demo@sha256:"
    "ebdb8d8b9b480f6ee7664ed3fdde8428767664f507d98f94090edeff04d7ebf2"
)
BUSYBOX = (
    "docker.io/library/busybox@sha256:"
    "73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
)

K8S_DOCUMENT = {
    "schemaVersion": 2,
    "name": "Kubernetes Sample Lab",
    "description": "One machine, two networks, one capability.",
    "networks": [
        {"name": "dmz", "subnet": "172.30.10.0/24", "gateway": "172.30.10.1"},
        {"name": "internal", "subnet": "172.30.20.0/24"},
    ],
    "workloads": [
        {
            "name": "web",
            "os": {"family": "linux", "version": "cirros-0.6"},
            "cpus": 1,
            "memoryMb": 256,
            "bootImage": CIRROS,
            "disks": [{"name": "root", "sizeGb": 1, "boot": True}],
            "interfaces": [
                {"network": "dmz", "ip": "172.30.10.5", "primary": True},
                {"network": "internal"},
            ],
        }
    ],
    "capabilities": [
        {
            "name": "podinfo",
            "version": "1.0.0",
            "scope": "shared",
            "chart": {
                "name": "podinfo",
                "version": "6.7.1",
                "repository": PERMITTED_REPOSITORY,
            },
            "values": {"replicaCount": 1},
            "hooks": {
                "verify": {"image": BUSYBOX, "command": ["/bin/sh", "-c", "true"]},
            },
        }
    ],
}

LEGACY_DOCUMENT = {
    "name": "Red Team Lab",
    "description": "A small Era A lab.",
    "networks": [{"name": "corp", "subnet": "10.10.0.0/24", "gateway": "10.10.0.1"}],
    "vms": [
        {
            "hostname": "dc-01",
            "base_image_tag": "ubuntu-22-04",
            "network_name": "corp",
            "ip_address": "10.10.0.10",
        }
    ],
}

K8S_INDEX_ENTRY = {
    "id": "k8s-sample-lab",
    "type": "blueprint",
    "name": "Kubernetes Sample Lab",
    "description": "One machine, two networks, one capability.",
    "path": "blueprints/k8s-sample-lab",
    "version": "1.0.0",
    "schemaVersion": 2,
}

LEGACY_INDEX_ENTRY = {
    "id": "red-team-lab",
    "type": "blueprint",
    "name": "Red Team Lab",
    "description": "A small Era A lab.",
    "path": "blueprints/red-team-lab",
    "version": "1.0.0",
}


def _write_catalog(root, *, k8s_document=None, legacy_document=None, k8s_entry=None):
    """A catalog holding one Era B item and one Era A item, as index.json plus two documents."""
    entries = []
    for entry, document in (
        (dict(k8s_entry or K8S_INDEX_ENTRY), k8s_document or K8S_DOCUMENT),
        (dict(LEGACY_INDEX_ENTRY), legacy_document or LEGACY_DOCUMENT),
    ):
        item_dir = root / entry["path"]
        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / "blueprint.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
        entries.append(entry)
    (root / "index.json").write_text(
        json.dumps({"catalog": {"name": "Two Era Catalog", "version": "1.0"}, "items": entries}),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def catalog(tmp_path):
    return _write_catalog(tmp_path / "catalog")


@pytest.fixture
def source(db_session, catalog):
    row = CatalogSource(
        name="Two Era Catalog", source_type=CatalogSourceType.LOCAL, url=str(catalog)
    )
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def install_on(monkeypatch, db_session):
    """A CatalogService on a chosen substrate with a chosen chart-repository allow-list.

    Both come from `Settings`, and `get_settings` is cached process-wide, so the object is patched
    in rather than the environment: a cached Settings built from a test's environment would leak
    into every test that ran after it. `capability.specs.repository_policy` imports `get_settings`
    from the config module at call time, which is why that module is patched as well as the
    service's own import of it.
    """

    def configure(substrate, repositories=""):
        settings = Settings(range_substrate=substrate, capability_chart_repositories=repositories)
        monkeypatch.setattr(catalog_service_module, "get_settings", lambda: settings)
        monkeypatch.setattr(config_module, "get_settings", lambda: settings)
        return CatalogService(db_session)

    return configure


# --- a v2 item survives the install ----------------------------------------------------------


def test_a_kubernetes_item_installs_with_its_workloads_and_capabilities_intact(
    install_on, source, db_session
):
    """The finding itself: the stored config used to be Era A keys and nothing else."""
    service = install_on(SUBSTRATE_KUBERNETES, PERMITTED_REPOSITORY)

    installed = service.install_item(source, "k8s-sample-lab", USER_ID, build_images=False)

    blueprint = (
        db_session.query(RangeBlueprint)
        .filter(RangeBlueprint.id == installed.local_resource_id)
        .first()
    )
    config = blueprint.config
    assert config["schemaVersion"] == SCHEMA_VERSION_K8S
    assert [w["name"] for w in config["workloads"]] == ["web"]
    assert config["workloads"][0]["bootImage"] == CIRROS
    assert [n["name"] for n in config["networks"]] == ["dmz", "internal"]
    # Scope and values specifically: a capability with no scope is refused at deploy, and values
    # are what make the chart the exercise rather than the vendor's default.
    assert config["capabilities"][0]["scope"] == "shared"
    assert config["capabilities"][0]["values"] == {"replicaCount": 1}


def test_the_installed_kubernetes_blueprint_is_deployable(install_on, source, db_session):
    """Deployable as the substrate itself judges it, not as this test re-implements it."""
    service = install_on(SUBSTRATE_KUBERNETES, PERMITTED_REPOSITORY)
    installed = service.install_item(source, "k8s-sample-lab", USER_ID, build_images=False)

    blueprint = (
        db_session.query(RangeBlueprint)
        .filter(RangeBlueprint.id == installed.local_resource_id)
        .first()
    )
    spec = read_blueprint(blueprint.config)

    assert spec.deployable_on_kubernetes
    assert spec.workloads[0].boot_image == CIRROS
    assert spec.workloads[0].cpus == 1
    assert spec.capabilities[0].scope is Scope.SHARED
    assert spec.capabilities[0].chart.repository == PERMITTED_REPOSITORY


def test_a_blueprint_yaml_that_is_not_a_mapping_is_refused_by_name():
    """A catalog file is remote content: a list or a bare string must not become a 500."""
    with pytest.raises(ValueError, match="not a mapping"):
        build_config_from_yaml(["not", "a", "blueprint"], item_name="Nonsense Lab")


def test_a_malformed_kubernetes_item_is_refused_by_name_not_stored_half_read(
    tmp_path, db_session, install_on
):
    broken = {**K8S_DOCUMENT, "workloads": [dict(K8S_DOCUMENT["workloads"][0])]}
    del broken["workloads"][0]["cpus"]
    root = _write_catalog(tmp_path / "broken-catalog", k8s_document=broken)
    row = CatalogSource(name="Broken", source_type=CatalogSourceType.LOCAL, url=str(root))
    db_session.add(row)
    db_session.commit()

    service = install_on(SUBSTRATE_KUBERNETES, PERMITTED_REPOSITORY)
    with pytest.raises(ValueError) as refusal:
        service.install_item(row, "k8s-sample-lab", USER_ID, build_images=False)

    assert "Kubernetes Sample Lab" in str(refusal.value)
    assert "cpus" in str(refusal.value)
    assert db_session.query(RangeBlueprint).count() == 0


# --- v1 still behaves exactly as it did, on the substrate that runs it -------------------------


def test_an_era_a_item_is_unchanged_on_docker(install_on, source, db_session):
    """The catalog is shared with Era A: the same file must install as it always has."""
    service = install_on(SUBSTRATE_DOCKER)

    installed = service.install_item(source, "red-team-lab", USER_ID, build_images=False)

    blueprint = (
        db_session.query(RangeBlueprint)
        .filter(RangeBlueprint.id == installed.local_resource_id)
        .first()
    )
    expected = build_config_from_yaml(LEGACY_DOCUMENT)
    assert "schemaVersion" not in blueprint.config
    assert blueprint.config["vms"] == expected["vms"]
    assert blueprint.config["networks"] == expected["networks"]
    assert blueprint.config["vms"][0]["base_image_tag"] == "ubuntu-22-04"


def test_an_era_a_item_is_refused_by_name_on_kubernetes(install_on, source, db_session):
    """Refused at install, not installed and then found undeployable."""
    service = install_on(SUBSTRATE_KUBERNETES)

    with pytest.raises(ValueError) as refusal:
        service.install_item(source, "red-team-lab", USER_ID, build_images=False)

    message = str(refusal.value)
    assert "Red Team Lab" in message
    assert "Era A blueprint" in message
    assert db_session.query(RangeBlueprint).count() == 0


def test_a_kubernetes_item_is_refused_by_name_on_docker(install_on, source, db_session):
    service = install_on(SUBSTRATE_DOCKER, PERMITTED_REPOSITORY)

    with pytest.raises(ValueError) as refusal:
        service.install_item(source, "k8s-sample-lab", USER_ID, build_images=False)

    message = str(refusal.value)
    assert "Kubernetes Sample Lab" in message
    assert "Kubernetes blueprint" in message
    assert db_session.query(RangeBlueprint).count() == 0


def test_a_capability_declared_by_an_era_a_item_is_carried_through(
    install_on, tmp_path, db_session
):
    """A capability is era-neutral, and dropping it silently is half the finding."""
    legacy_with_capability = {**LEGACY_DOCUMENT, "capabilities": K8S_DOCUMENT["capabilities"]}
    root = _write_catalog(tmp_path / "mixed-catalog", legacy_document=legacy_with_capability)
    row = CatalogSource(name="Mixed", source_type=CatalogSourceType.LOCAL, url=str(root))
    db_session.add(row)
    db_session.commit()

    service = install_on(SUBSTRATE_DOCKER, PERMITTED_REPOSITORY)
    installed = service.install_item(row, "red-team-lab", USER_ID, build_images=False)

    blueprint = (
        db_session.query(RangeBlueprint)
        .filter(RangeBlueprint.id == installed.local_resource_id)
        .first()
    )
    assert [c["name"] for c in blueprint.config["capabilities"]] == ["podinfo"]
    assert blueprint.config["vms"][0]["hostname"] == "dc-01"


# --- the chart repository is judged before the install, not at deploy --------------------------


def test_an_unpermitted_chart_repository_is_refused_in_the_install_plan():
    plan = resolve_install_plan(
        K8S_INDEX_ENTRY,
        index_items=[K8S_INDEX_ENTRY],
        installed_item_ids=set(),
        available_image_dirs=set(),
        document=K8S_DOCUMENT,
        substrate=SUBSTRATE_KUBERNETES,
        permitted_repositories=["https://charts.elsewhere.test"],
    )

    assert not plan.support.supported
    blocked = plan.blocked_capabilities
    assert [c.name for c in blocked] == ["podinfo"]
    assert PERMITTED_REPOSITORY in blocked[0].note
    assert "podinfo" in plan.support.reason


def test_a_permitted_chart_repository_leaves_the_plan_supported():
    plan = resolve_install_plan(
        K8S_INDEX_ENTRY,
        index_items=[K8S_INDEX_ENTRY],
        installed_item_ids=set(),
        available_image_dirs=set(),
        document=K8S_DOCUMENT,
        substrate=SUBSTRATE_KUBERNETES,
        permitted_repositories=["https://charts.example.test"],
    )

    assert plan.support.supported
    assert plan.blocked_capabilities == []
    assert [c.repository for c in plan.capabilities] == [PERMITTED_REPOSITORY]


def test_an_unpermitted_chart_repository_is_also_refused_at_install(install_on, source):
    """The plan is advice; this is the check that cannot be walked past."""
    service = install_on(SUBSTRATE_KUBERNETES, "https://charts.elsewhere.test")

    with pytest.raises(ValueError) as refusal:
        service.install_item(source, "k8s-sample-lab", USER_ID, build_images=False)

    assert PERMITTED_REPOSITORY in str(refusal.value)
    assert "capability_chart_repositories" in str(refusal.value)


# --- a v2 item's dependencies are disk images and charts, not image directories ----------------


def test_a_kubernetes_item_depends_on_its_disk_images_not_on_docker_image_directories():
    entry = {**K8S_INDEX_ENTRY, "requires_images": ["custom-web"], "requires_base_images": ["u22"]}
    plan = resolve_install_plan(
        entry,
        index_items=[entry],
        installed_item_ids=set(),
        available_image_dirs=set(),
        document=K8S_DOCUMENT,
        substrate=SUBSTRATE_KUBERNETES,
        permitted_repositories=[PERMITTED_REPOSITORY],
    )

    assert [s.kind for s in plan.steps if s.kind in (IMAGE, BASE_IMAGE)] == []
    assert any("custom-web" in w and "u22" in w for w in plan.warnings)
    assert [(d.workload, d.image) for d in plan.disk_images] == [("web", CIRROS)]
    assert plan.support.supported


def test_an_era_a_item_still_gets_its_docker_image_steps():
    """The v2 branch must not have taken the Era A path away from the era that uses it."""
    entry = {**LEGACY_INDEX_ENTRY, "requires_images": ["custom-web"]}
    plan = resolve_install_plan(
        entry,
        index_items=[entry],
        installed_item_ids=set(),
        available_image_dirs={"custom-web"},
        document=LEGACY_DOCUMENT,
    )

    assert [s.key for s in plan.steps] == ["image/custom-web", "blueprint/red-team-lab"]
    assert plan.disk_images == []


def test_a_machine_with_a_boot_disk_and_no_image_is_refused_before_the_install():
    workload = {k: v for k, v in K8S_DOCUMENT["workloads"][0].items() if k != "bootImage"}
    document = {**K8S_DOCUMENT, "workloads": [workload]}

    plan = resolve_install_plan(
        K8S_INDEX_ENTRY,
        index_items=[K8S_INDEX_ENTRY],
        installed_item_ids=set(),
        available_image_dirs=set(),
        document=document,
        substrate=SUBSTRATE_KUBERNETES,
        permitted_repositories=[PERMITTED_REPOSITORY],
    )

    assert not plan.support.supported
    assert [d.workload for d in plan.blocked_disk_images] == ["web"]
    assert "web" in plan.support.reason


def test_a_boot_image_named_by_tag_is_reported_but_not_refused():
    """A tag resolves on a connected install; it is the air-gapped one that cannot."""
    workload = {**K8S_DOCUMENT["workloads"][0], "bootImage": "quay.io/kubevirt/cirros:v1.9.0"}
    document = {**K8S_DOCUMENT, "workloads": [workload]}

    plan = resolve_install_plan(
        K8S_INDEX_ENTRY,
        index_items=[K8S_INDEX_ENTRY],
        installed_item_ids=set(),
        available_image_dirs=set(),
        document=document,
        substrate=SUBSTRATE_KUBERNETES,
        permitted_repositories=[PERMITTED_REPOSITORY],
    )

    assert plan.support.supported
    assert plan.blocked_disk_images == []
    assert any("digest" in w for w in plan.warnings)


# --- what the storefront reads to mark an item before anyone installs it -----------------------


def test_the_schema_version_reported_distinguishes_declared_from_unstated():
    assert catalog_item_schema_version({"schemaVersion": 2}) == 2
    assert catalog_item_schema_version({"schemaVersion": 1}) == 1
    assert catalog_item_schema_version({}) is None
    # Not an integer: reported as unstated rather than guessed at. The install refuses it by name.
    assert catalog_item_schema_version({"schemaVersion": "2"}) is None
    assert catalog_item_schema_version({"schemaVersion": True}) is None
    assert catalog_item_schema_version(None) is None


def test_item_detail_reads_the_era_from_the_document_not_only_the_index(install_on, source):
    """The index is hand-written and can be silent; the document is what the installer reads."""
    service = install_on(SUBSTRATE_KUBERNETES, PERMITTED_REPOSITORY)
    index = service._load_index(source)
    by_id = {item["id"]: item for item in index["items"]}

    assert service._item_schema_version(source, "k8s-sample-lab", by_id["k8s-sample-lab"]) == 2
    # The Era A entry declares nothing, and a blueprint that declares nothing is Era A -- a
    # definite 1, not "the index did not say".
    assert catalog_item_schema_version(by_id["red-team-lab"]) is None
    assert service._item_schema_version(source, "red-team-lab", by_id["red-team-lab"]) == 1
