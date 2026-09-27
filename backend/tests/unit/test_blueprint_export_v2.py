"""Exporting and re-importing a blueprint, whichever era wrote it.

A range definition that cannot leave the install is a range definition nobody else can reproduce
-- and a bug travels between the platform author and the content author as a blueprint export, so
an export path that refuses every Kubernetes blueprint is a bug report that cannot be sent. Before
this, `export_blueprint` validated the stored config against the Era A model, which has no field
for a workload or a capability package: a v2 blueprint came back "not found".

Three things are held here:

  * a v2 blueprint survives export and import with its workloads, its capabilities and their
    scopes intact, and the config that lands in the database is the document that left;
  * a v1 blueprint still round-trips exactly as it did, on either substrate;
  * a package format this engine has no code for is refused by name, rather than read as far as
    it happens to parse and reported as a success.

The service is called directly, the way `test_blueprint_v2_api` calls its routes: real database,
no app startup, no cluster and no Docker daemon.
"""

import copy
import json
import uuid
import zipfile
from pathlib import Path

import pytest

from proving_ground import config as config_module
from proving_ground.capability import specs as capability_specs
from proving_ground.models.blueprint import RangeBlueprint
from proving_ground.models.user import User, UserRole
from proving_ground.schemas.blueprint_export import (
    PACKAGE_FORMAT_KUBERNETES,
    PACKAGE_FORMAT_LEGACY,
    BlueprintExportOptions,
    BlueprintImportOptions,
)
from proving_ground.services.blueprint_export_service import BlueprintExportService

CIRROS = "quay.io/kubevirt/cirros-container-disk-demo@sha256:" + "e" * 64
BUSYBOX = "docker.io/library/busybox@sha256:" + "b" * 64
CHART_REPOSITORY = "https://charts.internal.example/mirror"


def v2_config():
    return {
        "schemaVersion": 2,
        "networks": [
            {"name": "dmz", "subnet": "172.30.10.0/24", "gateway": "172.30.10.1"},
            {"name": "internal", "subnet": "172.30.20.0/24"},
        ],
        "workloads": [
            {
                "name": "web",
                "os": {"family": "linux", "version": "cirros"},
                "cpus": 1,
                "memoryMb": 256,
                "bootImage": CIRROS,
                "disks": [{"name": "root", "sizeGb": 1, "boot": True}],
                "interfaces": [
                    {"network": "dmz", "ip": "172.30.10.5", "primary": True},
                    {"network": "internal", "ip": "172.30.20.5"},
                ],
            }
        ],
        "capabilities": [
            {
                "name": "inventory",
                "version": "1.0.0",
                "scope": "per-learner",
                "chart": {
                    "name": "inventory",
                    "version": "6.7.1",
                    "repository": CHART_REPOSITORY,
                },
                "values": {"replicaCount": 1, "seed": {"depot": "north"}},
                "hooks": {"verify": {"image": BUSYBOX, "command": ["/bin/true"]}},
            }
        ],
        "content_ids": ["7c9e4f1a-0000-4000-8000-00000000000a"],
    }


def v1_config():
    return {
        "networks": [{"name": "lan", "subnet": "10.1.0.0/24", "gateway": "10.1.0.1"}],
        "vms": [
            {
                "hostname": "a",
                "network_name": "lan",
                "ip_address": "10.1.0.5",
                "base_image_tag": "proving-ground/ubuntu:22.04",
            }
        ],
    }


@pytest.fixture(autouse=True)
def permit_the_chart_repository(monkeypatch):
    """This install permits the repository the fixture names.

    Which chart repositories a blueprint may name is the install's decision and it is enforced
    wherever a capability is parsed, import included. These tests are about the package format,
    so they configure an install that permits theirs rather than asserting on a policy they are
    not testing. `raising=False` because the policy is younger than the export path and an older
    checkout has no such function to replace.
    """
    monkeypatch.setattr(
        capability_specs,
        "repository_policy",
        lambda: capability_specs.ChartRepositoryPolicy(allowed=(CHART_REPOSITORY,)),
        raising=False,
    )


@pytest.fixture
def substrate(monkeypatch):
    """Point the install at one substrate or the other, the way RANGE_SUBSTRATE does."""

    def configure(name: str) -> None:
        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "range_substrate", name)

    return configure


@pytest.fixture
def author(db_session):
    tag = uuid.uuid4().hex[:8]
    user = User(
        username=f"author-{tag}",
        email=f"author-{tag}@x.invalid",
        hashed_password="x",
        role=UserRole.ADMIN,
        is_active=True,
        is_approved=True,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def blueprint(db_session, author):
    def make(config, *, name="bp"):
        stored = RangeBlueprint(
            name=name, version=1, config=copy.deepcopy(config), created_by=author.id
        )
        db_session.add(stored)
        db_session.commit()
        return stored

    return make


@pytest.fixture
def service():
    return BlueprintExportService()


def package_of(archive_path: Path) -> dict:
    with zipfile.ZipFile(archive_path) as zf:
        return json.loads(zf.read("blueprint.json"))


def round_trip(service, db_session, author, source, *, new_name, options=None):
    """Export `source` and import the package back, returning the stored blueprint."""
    archive_path, _ = service.export_blueprint(
        blueprint_id=source.id, user=author, db=db_session, options=options
    )
    result = service.import_blueprint(
        archive_path=archive_path,
        options=BlueprintImportOptions(new_name=new_name),
        user=author,
        db=db_session,
    )
    assert result.success, result.errors
    return db_session.query(RangeBlueprint).filter(RangeBlueprint.id == result.blueprint_id).first()


class TestAKubernetesBlueprintLeavesTheInstall:
    def test_export_writes_the_config_as_written(self, service, db_session, author, blueprint):
        source = blueprint(v2_config())

        archive_path, filename = service.export_blueprint(
            blueprint_id=source.id, user=author, db=db_session
        )

        assert filename.endswith(".zip")
        package = package_of(archive_path)
        # The whole point: byte-for-byte the document that was stored, not what an Era A model
        # can see of it.
        assert package["blueprint"]["config"] == v2_config()

    def test_manifest_describes_what_is_inside(self, service, db_session, author, blueprint):
        source = blueprint(v2_config())

        archive_path, _ = service.export_blueprint(
            blueprint_id=source.id, user=author, db=db_session
        )

        manifest = package_of(archive_path)["manifest"]
        assert manifest["version"] == PACKAGE_FORMAT_KUBERNETES
        assert manifest["blueprint_schema_version"] == 2
        assert (manifest["workload_count"], manifest["capability_count"]) == (1, 1)
        # Nothing Docker-shaped is claimed, because nothing Docker-shaped exists to carry.
        assert manifest["dockerfile_count"] == 0
        assert manifest["docker_images_included"] is False

    def test_import_keeps_workloads_capabilities_and_scopes(
        self, service, db_session, author, blueprint
    ):
        source = blueprint(v2_config())

        imported = round_trip(service, db_session, author, source, new_name="bp-imported")

        assert imported.config == v2_config()
        assert imported.config["workloads"][0]["interfaces"][1]["network"] == "internal"
        capability = imported.config["capabilities"][0]
        assert capability["scope"] == "per-learner"
        assert capability["values"] == {"replicaCount": 1, "seed": {"depot": "north"}}
        assert imported.config["content_ids"] == v2_config()["content_ids"]

    def test_validation_reports_the_package_in_era_b_words(
        self, service, db_session, author, blueprint
    ):
        source = blueprint(v2_config())
        archive_path, _ = service.export_blueprint(
            blueprint_id=source.id, user=author, db=db_session
        )

        validation = service.validate_import(archive_path, db_session)

        assert validation.blueprint_schema_version == 2
        assert validation.included_workloads == ["web"]
        assert validation.included_networks == ["dmz", "internal"]
        # Scope travels beside the name: an importer who cannot see it does not know whether a
        # reset here touches one learner or all of them.
        assert validation.included_capabilities == ["inventory (per-learner)"]
        assert validation.errors == []

    def test_an_unreadable_capability_is_named_rather_than_imported(
        self, service, db_session, author, blueprint
    ):
        broken = v2_config()
        del broken["capabilities"][0]["scope"]
        source = blueprint(broken)
        archive_path, _ = service.export_blueprint(
            blueprint_id=source.id, user=author, db=db_session
        )

        validation = service.validate_import(archive_path, db_session)

        assert validation.valid is False
        assert any("scope" in error for error in validation.errors)


class TestTheEraABlueprintIsUnchanged:
    def test_round_trip_preserves_the_v1_config(
        self, service, db_session, author, blueprint, substrate
    ):
        substrate("dind")
        source = blueprint(v1_config())

        imported = round_trip(service, db_session, author, source, new_name="v1-imported")

        assert imported.config == v1_config()

    def test_a_v1_package_still_declares_the_older_format(
        self, service, db_session, author, blueprint
    ):
        source = blueprint(v1_config())

        archive_path, _ = service.export_blueprint(
            blueprint_id=source.id, user=author, db=db_session
        )

        manifest = package_of(archive_path)["manifest"]
        # Stamping an Era A package 5.0 would strand it on every install that has not upgraded,
        # for a format change it does not use.
        assert manifest["version"] == PACKAGE_FORMAT_LEGACY
        assert manifest["blueprint_schema_version"] == 1

    def test_a_v1_capability_block_survives_the_round_trip(
        self, service, db_session, author, blueprint
    ):
        # Capability packages are era-neutral: a v1 blueprint may declare one, and the Era A
        # model has no field for it, so validating the config on the way out dropped it.
        config = v1_config()
        config["capabilities"] = copy.deepcopy(v2_config()["capabilities"])
        source = blueprint(config)

        imported = round_trip(service, db_session, author, source, new_name="v1-capability")

        assert imported.config["capabilities"][0]["scope"] == "per-learner"

    def test_a_vm_with_no_image_source_is_still_refused(
        self, service, db_session, author, blueprint
    ):
        config = v1_config()
        del config["vms"][0]["base_image_tag"]
        source = blueprint(config)
        archive_path, _ = service.export_blueprint(
            blueprint_id=source.id, user=author, db=db_session
        )

        validation = service.validate_import(archive_path, db_session)

        assert validation.valid is False
        assert validation.errors == ["VM 'a' has no image source or fallback"]

    def test_renaming_does_not_waive_the_refusal(self, service, db_session, author, blueprint):
        # A name conflict is reported as a conflict, never as an error, so `new_name` answers
        # nothing in that list -- and treating it as a waiver imported an invalid package.
        config = v1_config()
        del config["vms"][0]["base_image_tag"]
        source = blueprint(config)
        archive_path, _ = service.export_blueprint(
            blueprint_id=source.id, user=author, db=db_session
        )

        result = service.import_blueprint(
            archive_path=archive_path,
            options=BlueprintImportOptions(new_name="renamed-anyway"),
            user=author,
            db=db_session,
        )

        assert result.success is False
        assert result.errors == ["VM 'a' has no image source or fallback"]
        assert (
            db_session.query(RangeBlueprint).filter(RangeBlueprint.name == "renamed-anyway").first()
            is None
        )


class TestAPackageThisEngineCannotRead:
    def rewrite_version(self, archive_path: Path, version: str, written_by: str) -> Path:
        package = package_of(archive_path)
        package["manifest"]["version"] = version
        package["manifest"]["proving_ground_version"] = written_by
        rewritten = archive_path.with_name("rewritten.zip")
        with zipfile.ZipFile(rewritten, "w") as zf:
            zf.writestr("blueprint.json", json.dumps(package))
        return rewritten

    def test_an_unknown_format_is_refused_by_name(self, service, db_session, author, blueprint):
        source = blueprint(v2_config())
        archive_path, _ = service.export_blueprint(
            blueprint_id=source.id, user=author, db=db_session
        )
        newer = self.rewrite_version(archive_path, "9.1", "0.99.0")

        validation = service.validate_import(newer, db_session)

        assert validation.valid is False
        message = " ".join(validation.errors)
        assert "9.1" in message
        # Naming what wrote it turns "this did not work" into "upgrade that host or this one".
        assert "0.99.0" in message

    def test_an_unknown_format_imports_nothing(self, service, db_session, author, blueprint):
        source = blueprint(v2_config())
        archive_path, _ = service.export_blueprint(
            blueprint_id=source.id, user=author, db=db_session
        )
        newer = self.rewrite_version(archive_path, "9.1", "0.99.0")

        result = service.import_blueprint(
            archive_path=newer,
            options=BlueprintImportOptions(new_name="from-the-future"),
            user=author,
            db=db_session,
        )

        assert result.success is False
        assert result.blueprint_id is None
        assert (
            db_session.query(RangeBlueprint)
            .filter(RangeBlueprint.name == "from-the-future")
            .first()
            is None
        )


class TestTheEraAStepsOnAKubernetesInstall:
    def test_image_tarballs_are_not_gathered_for_a_v2_blueprint(
        self, service, db_session, author, blueprint, substrate
    ):
        # `include_docker_images` reaches for a daemon. There is none here, and there is nothing
        # for it to find either -- so the option has to be ignored rather than attempted.
        substrate("kubernetes")
        source = blueprint(v2_config())

        archive_path, _ = service.export_blueprint(
            blueprint_id=source.id,
            user=author,
            db=db_session,
            options=BlueprintExportOptions(include_docker_images=True, include_dockerfiles=True),
        )

        manifest = package_of(archive_path)["manifest"]
        assert manifest["docker_images"] == []
        assert manifest["dockerfile_count"] == 0

    def test_size_estimate_answers_rather_than_raising(
        self, service, db_session, author, blueprint, substrate
    ):
        substrate("kubernetes")
        source = blueprint(v2_config())

        estimate = service.estimate_export_size(source.id, db_session, include_docker_images=True)

        assert estimate["docker_images"] == []
        assert estimate["docker_images_total_bytes"] == 0
        assert "error" not in estimate
