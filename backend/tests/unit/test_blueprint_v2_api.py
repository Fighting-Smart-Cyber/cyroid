"""The blueprint API when the blueprint is a v2 (Era B) one -- COSMOS PG-122.

Called directly, the way `test_kubernetes_range_endpoints` calls its routes: real DB, no app
startup, no cluster. Three things are under test, and all three are about the same mistake --
treating the v1 `BlueprintConfig` as the only shape a config can have:

  * a v2 blueprint must survive a round trip through POST, GET and PUT with its workloads and
    capability packages intact, rather than 500ing on read and being emptied on write;
  * update-from-range must refuse rather than overwrite a v2 blueprint with what an Era A
    extractor can see of a Kubernetes range, which is nothing;
  * none of it may change what a v1 blueprint does -- both substrates share these endpoints.
"""

import copy
import json
import uuid

import pytest
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from proving_ground.api import blueprints as blueprints_api
from proving_ground.api import kubernetes_ranges
from proving_ground.capability import specs
from proving_ground.models.blueprint import RangeBlueprint, RangeInstance
from proving_ground.models.network import Network
from proving_ground.models.range import Range, RangeStatus
from proving_ground.models.user import User, UserAttribute, UserRole
from proving_ground.schemas.blueprint import (
    BlueprintCreate,
    BlueprintDetailResponse,
    BlueprintUpdate,
)

CIRROS = "quay.io/kubevirt/cirros-container-disk-demo@sha256:" + "e" * 64
BUSYBOX = "docker.io/library/busybox@sha256:" + "b" * 64
CHART_REPOSITORY = "https://charts.example/demo"


@pytest.fixture(autouse=True)
def install_permits_the_chart_repository(monkeypatch):
    """This install permits the repository the configs below name -- SEC-025.

    Which repositories a blueprint may name is the install's decision, and an install that has
    named none refuses every config here before the API is reached at all. That refusal is
    `test_capability_repository_policy`'s subject; these tests are about the era a config
    declares, so the policy is configured rather than left at its default.
    """
    monkeypatch.setattr(
        specs,
        "repository_policy",
        lambda: specs.ChartRepositoryPolicy(allowed=(CHART_REPOSITORY,)),
    )


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
                "name": "podinfo",
                "version": "1.0.0",
                "scope": "shared",
                "chart": {
                    "name": "podinfo",
                    "version": "6.7.1",
                    "repository": CHART_REPOSITORY,
                },
                "values": {"replicaCount": 1},
                "hooks": {"verify": {"image": BUSYBOX, "command": ["/bin/true"]}},
            }
        ],
    }


def v1_config():
    return {
        "networks": [
            {"name": "lan", "subnet": "10.1.0.0/24", "gateway": "10.1.0.1"},
        ],
        "vms": [{"hostname": "a", "network_name": "lan", "ip_address": "10.1.0.5"}],
    }


@pytest.fixture
def admin(db_session):
    tag = uuid.uuid4().hex[:8]
    user = User(
        username=f"admin-{tag}",
        email=f"admin-{tag}@x.invalid",
        hashed_password="x",
        role=UserRole.ADMIN,
        is_active=True,
        is_approved=True,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def blueprint(db_session, admin):
    def make(config):
        bp = RangeBlueprint(name="bp", version=1, config=config, created_by=admin.id)
        db_session.add(bp)
        db_session.commit()
        return bp

    return make


@pytest.fixture
def instance_of(db_session, admin):
    """A range that is an instance of a blueprint, with one Era A network row to extract."""

    def make(bp, *, with_rows=True):
        rng = Range(name="r", created_by=admin.id, status=RangeStatus.DRAFT)
        db_session.add(rng)
        db_session.flush()
        if with_rows:
            db_session.add(
                Network(range_id=rng.id, name="lan", subnet="10.1.0.0/24", gateway="10.1.0.1")
            )
        db_session.add(
            RangeInstance(
                name="i",
                blueprint_id=bp.id,
                blueprint_version=bp.version,
                subnet_offset=0,
                instructor_id=admin.id,
                range_id=rng.id,
            )
        )
        db_session.commit()
        return rng

    return make


def stored(db_session, blueprint_id):
    db_session.expire_all()
    return db_session.query(RangeBlueprint).filter(RangeBlueprint.id == blueprint_id).first()


class TestAuthoringAKubernetesBlueprint:
    def test_post_stores_the_config_as_written(self, db_session, admin):
        config = v2_config()
        resp = blueprints_api.create_blueprint(
            BlueprintCreate(name="k8s", config=copy.deepcopy(config)), db_session, admin
        )
        assert resp.config == config
        assert stored(db_session, resp.id).config == config

    def test_post_counts_workloads_as_machines(self, db_session, admin):
        # A v2 blueprint has no `vms` key at all, so counting that reports it as empty.
        resp = blueprints_api.create_blueprint(
            BlueprintCreate(name="k8s", config=v2_config()), db_session, admin
        )
        assert (resp.network_count, resp.vm_count) == (2, 1)

    def test_get_returns_the_document_rather_than_500ing(self, db_session, admin, blueprint):
        bp = blueprint(v2_config())
        resp = blueprints_api.get_blueprint(bp.id, db_session, admin)
        assert resp.config == v2_config()
        assert resp.config["capabilities"][0]["chart"]["name"] == "podinfo"
        assert resp.vm_count == 1
        # The 500 was the response model, not the handler: FastAPI re-validates what a route
        # returns against `response_model`, and a v2 config has no `vms` for the v1 one to find.
        round_tripped = BlueprintDetailResponse.model_validate(jsonable_encoder(resp))
        assert round_tripped.config == v2_config()

    def test_list_counts_workloads_too(self, db_session, admin, blueprint):
        blueprint(v2_config())
        (resp,) = blueprints_api.list_blueprints(db_session, admin)
        assert (resp.network_count, resp.vm_count) == (2, 1)

    def test_put_keeps_workloads_and_capabilities_and_bumps_the_version(
        self, db_session, admin, blueprint
    ):
        bp = blueprint(v2_config())
        edited = v2_config()
        edited["workloads"][0]["memoryMb"] = 512
        edited["workloads"].append(
            {
                "name": "db",
                "os": {"family": "linux", "version": "cirros"},
                "cpus": 1,
                "memoryMb": 256,
                "bootImage": CIRROS,
                "disks": [{"name": "root", "sizeGb": 1, "boot": True}],
                "interfaces": [{"network": "internal", "ip": "172.30.20.6"}],
            }
        )
        resp = blueprints_api.update_blueprint(
            bp.id, BlueprintUpdate(config=copy.deepcopy(edited)), db_session, admin
        )
        assert resp.version == 2
        assert resp.config == edited
        assert [w["name"] for w in stored(db_session, bp.id).config["workloads"]] == ["web", "db"]
        assert len(stored(db_session, bp.id).config["capabilities"]) == 1

    def test_a_malformed_v2_config_is_a_422_quoting_the_reader(self, db_session, admin):
        config = v2_config()
        del config["capabilities"][0]["hooks"]["verify"]
        with pytest.raises(HTTPException) as exc:
            blueprints_api.create_blueprint(
                BlueprintCreate(name="k8s", config=config), db_session, admin
            )
        assert exc.value.status_code == 422
        assert "verify hook" in exc.value.detail

    def test_a_refused_edit_leaves_the_stored_config_alone(self, db_session, admin, blueprint):
        # The config used to be read back after the commit, so an edit the reader refuses saved
        # first and failed afterwards.
        bp = blueprint(v2_config())
        broken = v2_config()
        del broken["workloads"][0]["os"]
        with pytest.raises(HTTPException) as exc:
            blueprints_api.update_blueprint(
                bp.id, BlueprintUpdate(name="renamed", config=broken), db_session, admin
            )
        assert exc.value.status_code == 422
        after = stored(db_session, bp.id)
        assert after.version == 1 and after.name == "bp"
        assert after.config == v2_config()

    def test_a_field_of_the_wrong_json_type_is_a_422_not_a_500(self, db_session, admin):
        # The reader coerces what it reads, so an object where a number belongs comes back as
        # TypeError rather than ValueError. A config is hand-edited; the server is not at fault.
        config = v2_config()
        config["workloads"][0]["cpus"] = {"count": 2}
        with pytest.raises(HTTPException) as exc:
            blueprints_api.create_blueprint(
                BlueprintCreate(name="k8s", config=config), db_session, admin
            )
        assert exc.value.status_code == 422

    def test_a_config_from_a_newer_engine_is_refused_not_stored(self, db_session, admin):
        with pytest.raises(HTTPException) as exc:
            blueprints_api.create_blueprint(
                BlueprintCreate(name="future", config={"schemaVersion": 99}), db_session, admin
            )
        assert exc.value.status_code == 422 and "99" in exc.value.detail

    def test_post_needs_exactly_one_of_range_id_and_config(self):
        with pytest.raises(ValueError):
            BlueprintCreate(name="neither")
        with pytest.raises(ValueError):
            BlueprintCreate(name="both", range_id=uuid.uuid4(), config=v2_config())


class TestUpdateFromRangeCannotEmptyAKubernetesBlueprint:
    def test_refuses_with_409_and_changes_nothing(self, db_session, admin, blueprint, instance_of):
        bp = blueprint(v2_config())
        rng = instance_of(bp)
        before = json.dumps(stored(db_session, bp.id).config, sort_keys=True)

        with pytest.raises(HTTPException) as exc:
            blueprints_api.update_blueprint_from_range(bp.id, rng.id, db_session, admin)

        assert exc.value.status_code == 409
        after = stored(db_session, bp.id)
        assert json.dumps(after.config, sort_keys=True) == before
        assert after.version == 1

    def test_refuses_for_a_range_on_the_kubernetes_substrate(
        self, db_session, admin, blueprint, instance_of, monkeypatch
    ):
        # Even a v1 blueprint: on this substrate there are no rows to extract, so the extraction
        # would write an empty config over it.
        monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: True)
        bp = blueprint(v1_config())
        rng = instance_of(bp, with_rows=False)
        with pytest.raises(HTTPException) as exc:
            blueprints_api.update_blueprint_from_range(bp.id, rng.id, db_session, admin)
        assert exc.value.status_code == 409
        assert stored(db_session, bp.id).config == v1_config()

    def test_saving_a_kubernetes_range_as_a_blueprint_is_refused(
        self, db_session, admin, blueprint, instance_of, monkeypatch
    ):
        monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: True)
        bp = blueprint(v2_config())
        rng = instance_of(bp, with_rows=False)
        with pytest.raises(HTTPException) as exc:
            blueprints_api.create_blueprint(
                BlueprintCreate(name="copy", range_id=rng.id), db_session, admin
            )
        assert exc.value.status_code == 409
        assert db_session.query(RangeBlueprint).count() == 1


class TestTheEraABlueprintIsUnaffected:
    def test_post_from_a_range_still_extracts_rows(self, db_session, admin, blueprint, instance_of):
        rng = instance_of(blueprint(v1_config()))
        resp = blueprints_api.create_blueprint(
            BlueprintCreate(name="extracted", range_id=rng.id), db_session, admin
        )
        assert [n["name"] for n in resp.config["networks"]] == ["lan"]
        assert resp.config["vms"] == []
        assert (resp.network_count, resp.vm_count) == (1, 0)

    def test_get_returns_a_v1_config_and_counts_its_vms(self, db_session, admin, blueprint):
        bp = blueprint(v1_config())
        resp = blueprints_api.get_blueprint(bp.id, db_session, admin)
        assert [vm["hostname"] for vm in resp.config["vms"]] == ["a"]
        assert (resp.network_count, resp.vm_count) == (1, 1)

    def test_put_fills_the_v1_defaults_as_before(self, db_session, admin, blueprint):
        bp = blueprint(v1_config())
        resp = blueprints_api.update_blueprint(
            bp.id, BlueprintUpdate(config=v1_config()), db_session, admin
        )
        assert resp.version == 2
        # What BlueprintConfig adds on validation, which the Era A deploy path reads back.
        assert resp.config["networks"][0]["is_isolated"] is False
        assert resp.config["vms"][0]["cpu"] == 1

    def test_put_keeps_a_capability_block_the_v1_model_does_not_know(
        self, db_session, admin, blueprint
    ):
        # Capabilities are era-neutral and `read_blueprint` reads them from a v1 config too;
        # dumping the config through the v1 model alone would drop them on every edit.
        bp = blueprint(v1_config())
        config = v1_config()
        config["capabilities"] = v2_config()["capabilities"]
        resp = blueprints_api.update_blueprint(
            bp.id, BlueprintUpdate(config=config), db_session, admin
        )
        assert [c["name"] for c in resp.config["capabilities"]] == ["podinfo"]

    def test_put_still_refuses_an_invalid_v1_config(self, db_session, admin, blueprint):
        bp = blueprint(v1_config())
        broken = v1_config()
        del broken["networks"][0]["gateway"]
        with pytest.raises(HTTPException) as exc:
            blueprints_api.update_blueprint(
                bp.id, BlueprintUpdate(config=broken), db_session, admin
            )
        assert exc.value.status_code == 422
        assert stored(db_session, bp.id).version == 1

    def test_update_from_range_still_rewrites_a_v1_blueprint(
        self, db_session, admin, blueprint, instance_of
    ):
        bp = blueprint(v1_config())
        rng = instance_of(bp)
        resp = blueprints_api.update_blueprint_from_range(bp.id, rng.id, db_session, admin)
        assert resp.version == 2
        assert [n["name"] for n in resp.config["networks"]] == ["lan"]
        assert resp.config["vms"] == []

    def test_update_from_range_keeps_what_it_cannot_see(
        self, db_session, admin, blueprint, instance_of
    ):
        # Extraction answers out of rows, and a capability package has none -- so writing its
        # answer over the document deleted the blueprint's capabilities on an Era A install too.
        config = v1_config()
        config["capabilities"] = v2_config()["capabilities"]
        bp = blueprint(config)
        rng = instance_of(bp)
        resp = blueprints_api.update_blueprint_from_range(bp.id, rng.id, db_session, admin)
        assert [c["name"] for c in resp.config["capabilities"]] == ["podinfo"]
        assert [n["name"] for n in stored(db_session, bp.id).config["networks"]] == ["lan"]


class TestOnlyTheOwnerOrAnAdminMayChangeABlueprint:
    """The seeded blueprint has no creator, and on Kubernetes it is the only deployable one.

    `if blueprint.created_by and ...` read an absent creator as "everyone's": the config branch
    of PUT was the only route that checked at all, PUT's metadata branch did not, and DELETE had
    no check, so any authenticated account could rename or delete the install's only blueprint.
    """

    @pytest.fixture
    def student(self, db_session):
        tag = uuid.uuid4().hex[:8]
        user = User(
            username=f"student-{tag}",
            email=f"student-{tag}@x.invalid",
            hashed_password="x",
            role=UserRole.STUDENT,
            is_active=True,
            is_approved=True,
        )
        db_session.add(user)
        db_session.commit()
        return user

    @pytest.fixture
    def real_admin(self, db_session, admin):
        # `is_admin` reads the ABAC attributes, not the legacy `role` column, so the fixture
        # user has to carry the attribute to actually be one.
        db_session.add(
            UserAttribute(user_id=admin.id, attribute_type="role", attribute_value="admin")
        )
        db_session.commit()
        db_session.refresh(admin)
        return admin

    def _seed(self, db, owner_id=None):
        bp = RangeBlueprint(name="seeded", version=1, config=v2_config(), created_by=owner_id)
        db.add(bp)
        db.commit()
        db.refresh(bp)
        return bp

    def test_a_stranger_cannot_rename_an_unowned_blueprint(self, db_session, student):
        from proving_ground.api import blueprints as api
        from proving_ground.schemas.blueprint import BlueprintUpdate

        bp = self._seed(db_session)
        with pytest.raises(HTTPException) as exc:
            api.update_blueprint(bp.id, BlueprintUpdate(name="mine now"), db_session, student)
        assert exc.value.status_code == 403
        db_session.refresh(bp)
        assert bp.name == "seeded"

    def test_a_stranger_cannot_delete_an_unowned_blueprint(self, db_session, student):
        from proving_ground.api import blueprints as api

        bp = self._seed(db_session)
        with pytest.raises(HTTPException) as exc:
            api.delete_blueprint(bp.id, db_session, student)
        assert exc.value.status_code == 403

    def test_an_admin_may(self, db_session, real_admin):
        from proving_ground.api import blueprints as api
        from proving_ground.schemas.blueprint import BlueprintUpdate

        bp = self._seed(db_session)
        api.update_blueprint(bp.id, BlueprintUpdate(name="renamed"), db_session, real_admin)
        db_session.refresh(bp)
        assert bp.name == "renamed"

    def test_the_owner_may(self, db_session, student):
        from proving_ground.api import blueprints as api
        from proving_ground.schemas.blueprint import BlueprintUpdate

        bp = self._seed(db_session, owner_id=student.id)
        api.update_blueprint(bp.id, BlueprintUpdate(name="renamed"), db_session, student)
        db_session.refresh(bp)
        assert bp.name == "renamed"
