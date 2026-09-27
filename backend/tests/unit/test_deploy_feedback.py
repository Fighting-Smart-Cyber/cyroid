"""What a failed deploy, and a refused export, tell the person who asked for them.

Two defects, one theme -- the product knows why something did not work and does not say so, or
says it in a way that gives away more than it should.

  * A deploy from a blueprint answers the moment the task is enqueued. Everything after that
    reaches the user through exactly one field, `Range.error_message`, so that field has to
    carry a reason an instructor can act on: which step failed, how much of the range came up
    anyway, and whether the cluster is still holding objects that have to be torn down. The
    refusals the platform can see ahead of time should not get that far at all -- they belong in
    the endpoint's own answer.

  * An export that cannot read a blueprint must not quote the blueprint back. Pydantic renders
    a validation error with a repr of the input value, and here the input value is the stored
    document: hook commands, chart values, whatever the author put in it.

Called directly, as the other API and Era B service tests are: real DB, no app startup, no
cluster.
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from proving_ground.api import blueprints as blueprints_api
from proving_ground.api import kubernetes_ranges
from proving_ground.capability import kubernetes_client
from proving_ground.models.blueprint import RangeBlueprint, RangeInstance
from proving_ground.models.range import Range, RangeStatus
from proving_ground.models.user import User, UserRole
from proving_ground.schemas.blueprint import BlueprintConfig, InstanceDeploy
from proving_ground.services import kubernetes_range_service as svc

from .test_kubernetes_runtime import FakeKube

BUSYBOX = "docker.io/library/busybox@sha256:" + "a" * 64
CIRROS = "quay.io/kubevirt/cirros-container-disk-demo@sha256:" + "c" * 64

# Stands in for anything an author puts in a capability package -- a seed command, a chart value,
# a scenario parameter. Short enough that pydantic renders it whole rather than eliding its
# middle, so "did this reach the caller" is a question the assertions can actually answer.
SECRET_MARKER = "seed-cmd-secret"


def v2_config(*, workloads=1, capability_scope="shared"):
    return {
        "schemaVersion": 2,
        "networks": [{"name": "dmz", "subnet": "172.30.10.0/24", "gateway": "172.30.10.1"}],
        "workloads": [
            {
                "name": name,
                "os": {"family": "linux", "version": "cirros"},
                "cpus": 1,
                "memoryMb": 256,
                "bootImage": CIRROS,
                "disks": [{"name": "root", "sizeGb": 1, "boot": True}],
                "interfaces": [{"network": "dmz", "ip": f"172.30.10.{5 + i}", "primary": True}],
            }
            for i, name in enumerate(["web", "db"][:workloads])
        ],
        "capabilities": [
            {
                "name": "podinfo",
                "version": "1.0.0",
                "scope": capability_scope,
                "chart": {
                    "name": "podinfo",
                    "version": "6.7.1",
                    "repository": "https://stefanprodan.github.io/podinfo",
                },
                "hooks": {
                    "seed": {"image": BUSYBOX, "command": ["/bin/true"]},
                    "verify": {"image": BUSYBOX, "command": ["/bin/true"]},
                },
            }
        ],
    }


def v1_config():
    return {
        "networks": [{"name": "lan", "subnet": "10.1.0.0/24", "gateway": "10.1.0.1"}],
        "vms": [{"hostname": "a", "network_name": "lan", "ip_address": "10.1.0.5"}],
    }


@pytest.fixture
def kube(monkeypatch):
    fake = FakeKube()

    async def connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(kubernetes_client.KubernetesApiClient, "connect", connect)
    monkeypatch.setattr(svc.KubernetesApiClient, "connect", connect)
    return fake


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
def range_row(db_session, admin):
    """A range that is an instance of a blueprint, optionally with a learner assigned."""

    def make(config, *, assigned=True):
        tag = uuid.uuid4().hex[:8]
        learner = User(
            username=f"l-{tag}",
            email=f"l-{tag}@x.invalid",
            hashed_password="x",
            is_active=True,
            is_approved=True,
        )
        db_session.add(learner)
        db_session.flush()
        blueprint = RangeBlueprint(name="bp", version=1, config=config, created_by=admin.id)
        rng = Range(
            name="r",
            status=RangeStatus.DRAFT,
            created_by=admin.id,
            assigned_to_user_id=learner.id if assigned else None,
        )
        db_session.add_all([blueprint, rng])
        db_session.flush()
        db_session.add(
            RangeInstance(
                name="inst",
                blueprint_id=blueprint.id,
                blueprint_version=1,
                subnet_offset=0,
                instructor_id=admin.id,
                range_id=rng.id,
            )
        )
        db_session.commit()
        return rng

    return make


def reread(db_session, rng):
    db_session.expire_all()
    return db_session.query(Range).filter(Range.id == rng.id).first()


class TestAFailedDeploySaysWhy:
    async def test_the_range_is_left_in_error_with_the_step_that_failed(
        self, db_session, kube, range_row
    ):
        rng = range_row(v2_config())
        kube.vm_statuses["web"] = ["ErrImagePull"]

        with pytest.raises(svc.RangeDeployError) as raised:
            await svc.deploy_range_on_kubernetes(db_session, rng.id)

        row = reread(db_session, rng)
        assert row.status == RangeStatus.ERROR
        assert "waiting for the machines" in row.error_message
        assert "ErrImagePull" in row.error_message
        # The worker writes `str(exc)` onto this same field. The two must be the same words, or
        # the row says one thing and the deployment-failed event another.
        assert str(raised.value) == row.error_message

    async def test_the_message_counts_the_machines_that_did_come_up(
        self, db_session, kube, range_row
    ):
        rng = range_row(v2_config(workloads=2))
        kube.vm_statuses["db"] = ["ErrorUnschedulable"]

        with pytest.raises(svc.RangeDeployError):
            await svc.deploy_range_on_kubernetes(db_session, rng.id)

        message = reread(db_session, rng).error_message
        assert "1 of 2 machine(s) came up" in message
        assert "db is ErrorUnschedulable" in message

    async def test_the_message_says_the_cluster_is_still_holding_the_range(
        self, db_session, kube, range_row
    ):
        rng = range_row(v2_config())
        kube.vm_statuses["web"] = ["ErrImagePull"]

        with pytest.raises(svc.RangeDeployError):
            await svc.deploy_range_on_kubernetes(db_session, rng.id)

        message = reread(db_session, rng).error_message
        assert "still on the cluster" in message
        assert "tear the range down" in message

    async def test_a_failure_partway_through_creation_still_says_to_tear_it_down(
        self, db_session, kube, range_row
    ):
        # Creating the range is three cluster calls, not one: the namespace, then the default-deny
        # policy, then a vcluster's control plane. A failure in the second or third leaves the
        # namespace behind, and a message that says nothing was created is the reason nobody
        # tears it down before redeploying on top of it.
        rng = range_row(v2_config())

        async def refuse(**kwargs):
            raise RuntimeError("policy rejected by admission")

        kube.apply_network_policy = refuse

        with pytest.raises(svc.RangeDeployError):
            await svc.deploy_range_on_kubernetes(db_session, rng.id)

        assert kube.namespaces, "the namespace was created before the failure"
        assert "still on the cluster" in reread(db_session, rng).error_message

    async def test_a_failure_that_left_nothing_does_not_invent_a_teardown(
        self, db_session, kube, range_row
    ):
        # The other direction of the same read: sending an instructor to tear down a range that
        # has no namespace wastes their time and teaches them to ignore the sentence.
        rng = range_row(v2_config())

        async def refuse(name, labels):
            raise RuntimeError("namespace quota exceeded")

        kube.ensure_namespace = refuse

        with pytest.raises(svc.RangeDeployError):
            await svc.deploy_range_on_kubernetes(db_session, rng.id)

        message = reread(db_session, rng).error_message
        assert "namespace quota exceeded" in message
        assert "still on the cluster" not in message

    async def test_a_cluster_that_stops_answering_still_gets_the_reason_recorded(
        self, db_session, kube, range_row, monkeypatch
    ):
        # The message is built by reading the cluster back, and the cluster is often what failed.
        # An API server that accepts the connection and then answers nothing would otherwise hold
        # the deploy open indefinitely, leaving the range at DEPLOYING with nothing written on it
        # -- which is the state this whole message exists to end.
        rng = range_row(v2_config())
        monkeypatch.setattr(svc, "_READBACK_BUDGET_SECONDS", 0.01)

        async def never_answers(*args, **kwargs):
            await asyncio.sleep(30)

        async def fail_networks(self, *args, **kwargs):
            raise RuntimeError("attachment rejected")

        monkeypatch.setattr(svc.RangeLifecycle, "apply_networks", fail_networks)
        kube.namespace_exists = never_answers
        kube.get_custom_object = never_answers

        with pytest.raises(svc.RangeDeployError):
            await asyncio.wait_for(svc.deploy_range_on_kubernetes(db_session, rng.id), timeout=5)

        row = reread(db_session, rng)
        assert row.status == RangeStatus.ERROR
        assert "attachment rejected" in row.error_message

    async def test_a_capability_that_will_not_install_is_named(
        self, db_session, kube, range_row, monkeypatch
    ):
        rng = range_row(v2_config())

        async def refuse(self, spec, placement):
            raise RuntimeError("chart repository not permitted by this install")

        monkeypatch.setattr(svc.KubernetesRuntime, "install", refuse)

        with pytest.raises(svc.RangeDeployError):
            await svc.deploy_range_on_kubernetes(db_session, rng.id)

        message = reread(db_session, rng).error_message
        assert "installing the podinfo capability" in message
        assert "chart repository not permitted" in message

    async def test_a_refusal_before_the_cluster_is_recorded_in_plain_words(
        self, db_session, kube, range_row
    ):
        # An Era A blueprint on this substrate. Nothing was built, so the message describes the
        # refusal and nothing else -- and it names no internal issue key, which is what the text
        # used to end with and meant nothing to the instructor reading it.
        rng = range_row(v1_config())

        with pytest.raises(ValueError):
            await svc.deploy_range_on_kubernetes(db_session, rng.id)

        row = reread(db_session, rng)
        assert row.status == RangeStatus.ERROR
        assert "v2 blueprint" in row.error_message
        assert "MIG-" not in row.error_message
        assert "still on the cluster" not in row.error_message
        assert not kube.calls

    async def test_an_unreachable_cluster_blames_the_platform_not_the_range(
        self, db_session, range_row, monkeypatch
    ):
        rng = range_row(v2_config())

        async def unreachable(*args, **kwargs):
            raise ConnectionError("connection refused")

        monkeypatch.setattr(svc.KubernetesApiClient, "connect", unreachable)

        with pytest.raises(ConnectionError):
            await svc.deploy_range_on_kubernetes(db_session, rng.id)

        message = reread(db_session, rng).error_message
        assert "could not reach its Kubernetes cluster" in message
        assert "Nothing was created" in message

    async def test_a_failure_with_no_message_still_names_its_shape(
        self, db_session, kube, range_row, monkeypatch
    ):
        # `TimeoutError()` stringifies to the empty string, which would leave the instructor
        # reading "Deploy failed while creating the range's machines: ".
        rng = range_row(v2_config())

        async def time_out(self, *args, **kwargs):
            raise TimeoutError()

        monkeypatch.setattr(svc.RangeLifecycle, "apply_workloads", time_out)

        with pytest.raises(svc.RangeDeployError):
            await svc.deploy_range_on_kubernetes(db_session, rng.id)

        assert "TimeoutError" in reread(db_session, rng).error_message

    async def test_the_message_fits_the_column_that_has_to_hold_it(
        self, db_session, kube, range_row, monkeypatch
    ):
        rng = range_row(v2_config())

        async def shout(self, *args, **kwargs):
            raise RuntimeError("x" * 5000)

        monkeypatch.setattr(svc.RangeLifecycle, "apply_networks", shout)

        with pytest.raises(svc.RangeDeployError) as raised:
            await svc.deploy_range_on_kubernetes(db_session, rng.id)

        assert len(str(raised.value)) <= 1000

    async def test_a_deploy_that_works_still_works(self, db_session, kube, range_row):
        rng = range_row(v2_config())
        kube.vmi_interfaces["web"] = [{"name": "net1", "ipAddress": "172.30.10.5"}]

        result = await svc.deploy_range_on_kubernetes(db_session, rng.id)

        assert result["workloads"]["web"]["status"] == "Running"
        assert result["capabilities"] == ["podinfo"]
        assert reread(db_session, rng).error_message is None
        assert kube.closed


class TestDeployingFromABlueprintRefusesUpFront:
    @pytest.fixture
    def era_b(self, monkeypatch):
        monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: True)

    @pytest.fixture
    def enqueued(self, monkeypatch):
        sent = []

        class Task:
            @staticmethod
            def send(range_id):
                sent.append(range_id)

        monkeypatch.setattr(blueprints_api, "deploy_range_task", Task)
        return sent

    def blueprint(self, db_session, admin, config):
        bp = RangeBlueprint(name="bp", version=1, config=config, created_by=admin.id)
        db_session.add(bp)
        db_session.commit()
        return bp

    def test_an_era_a_blueprint_answers_400_instead_of_201_and_an_error_later(
        self, db_session, admin, era_b, enqueued
    ):
        bp = self.blueprint(db_session, admin, v1_config())

        with pytest.raises(HTTPException) as raised:
            blueprints_api.deploy_instance(
                bp.id, InstanceDeploy(name="i", auto_deploy=True), db_session, admin
            )

        assert raised.value.status_code == 400
        assert "v2 blueprint" in raised.value.detail
        assert enqueued == []

    def test_the_refused_deploy_leaves_no_range_and_no_instance_behind(
        self, db_session, admin, era_b, enqueued
    ):
        # The rows are flushed rather than committed precisely so this holds. A 201 followed by
        # a worker failure at least left something the user could delete; a 400 that strands a
        # range and an instance leaves rows nobody asked for.
        bp = self.blueprint(db_session, admin, v1_config())

        with pytest.raises(HTTPException):
            blueprints_api.deploy_instance(
                bp.id, InstanceDeploy(name="i", auto_deploy=True), db_session, admin
            )

        db_session.expire_all()
        assert db_session.query(Range).filter(Range.name == "i").count() == 0
        assert db_session.query(RangeInstance).filter(RangeInstance.name == "i").count() == 0

    def test_a_per_learner_capability_with_nobody_assigned_is_refused_here_too(
        self, db_session, admin, era_b, enqueued
    ):
        # An instance created from the Blueprints page is assigned to nobody, so this blueprint
        # can never deploy from here. Saying so beats a range that reads "deploying" until the
        # worker gives up on it.
        bp = self.blueprint(db_session, admin, v2_config(capability_scope="per-learner"))

        with pytest.raises(HTTPException) as raised:
            blueprints_api.deploy_instance(
                bp.id, InstanceDeploy(name="i", auto_deploy=True), db_session, admin
            )

        assert raised.value.status_code == 400
        # The refusal has to name the way forward, not only the problem: deploying a per-learner
        # blueprint from the Blueprints page is a door that cannot work, and a message that
        # stops at "no assigned learner" leaves the instructor with nowhere to go.
        assert "per-learner" in raised.value.detail
        assert "training event" in raised.value.detail
        assert enqueued == []

    def test_a_deployable_blueprint_is_still_created_and_still_enqueued(
        self, db_session, admin, era_b, enqueued
    ):
        bp = self.blueprint(db_session, admin, v2_config())

        resp = blueprints_api.deploy_instance(
            bp.id, InstanceDeploy(name="i", auto_deploy=True), db_session, admin
        )

        assert resp.name == "i"
        assert enqueued == [str(resp.range_id)]
        assert db_session.query(Range).filter(Range.id == resp.range_id).first() is not None

    def test_without_auto_deploy_nothing_is_validated_and_nothing_is_enqueued(
        self, db_session, admin, era_b, enqueued
    ):
        # Creating the instance to assign a learner to it afterwards is the documented way past
        # the refusal above, so it must not itself be refused.
        bp = self.blueprint(db_session, admin, v2_config(capability_scope="per-learner"))

        resp = blueprints_api.deploy_instance(
            bp.id, InstanceDeploy(name="i", auto_deploy=False), db_session, admin
        )

        assert resp.range_id is not None
        assert enqueued == []

    def test_an_era_a_host_deploys_exactly_as_it_did(self, db_session, admin, monkeypatch):
        # Docker hosts share this endpoint and must not acquire a Kubernetes-shaped refusal.
        monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: False)
        sent = []
        monkeypatch.setattr(
            blueprints_api, "deploy_range_task", type("T", (), {"send": staticmethod(sent.append)})
        )
        bp = self.blueprint(db_session, admin, v1_config())

        resp = blueprints_api.deploy_instance(
            bp.id, InstanceDeploy(name="i", auto_deploy=True), db_session, admin
        )

        assert sent == [str(resp.range_id)]


def call_export(blueprint_id, db, user, **overrides):
    """`export_blueprint` with its query parameters spelled out.

    Called directly, nothing resolves FastAPI's `Query(...)` defaults, so the options model would
    be handed `Query` objects rather than booleans and refuse before the handler did anything.
    """
    args = {
        "include_msel": True,
        "include_dockerfiles": True,
        "include_docker_images": False,
        "include_content": True,
        "include_artifacts": False,
        "content_id": None,
        **overrides,
    }
    return blueprints_api.export_blueprint(blueprint_id, db, user, **args)


def validation_error_quoting_the_config() -> ValidationError:
    """A real pydantic failure whose input value is a blueprint document, marker and all."""
    try:
        BlueprintConfig.model_validate({"seedCommand": SECRET_MARKER})
    except ValidationError as exc:
        assert SECRET_MARKER in str(exc), "the marker must survive pydantic's truncation"
        return exc
    raise AssertionError("that config should not have validated")


class TestExportRefusesWithoutQuotingTheBlueprint:
    @pytest.fixture
    def blueprint(self, db_session, admin):
        bp = RangeBlueprint(name="bp", version=1, config=v2_config(), created_by=admin.id)
        db_session.add(bp)
        db_session.commit()
        return bp

    def test_export_size_answers_a_fixed_message_with_no_config_in_it(
        self, db_session, admin, blueprint, monkeypatch
    ):
        service = blueprints_api.get_blueprint_export_service()

        def refuse(**kwargs):
            raise validation_error_quoting_the_config()

        monkeypatch.setattr(service, "estimate_export_size", refuse)
        monkeypatch.setattr(blueprints_api, "get_blueprint_export_service", lambda: service)

        with pytest.raises(HTTPException) as raised:
            blueprints_api.get_export_size(blueprint.id, db_session, admin)

        assert raised.value.status_code == 409
        assert SECRET_MARKER not in str(raised.value.detail)
        assert "seedCommand" not in str(raised.value.detail)
        assert "schema v2" in raised.value.detail

    def test_export_answers_a_fixed_message_with_no_config_in_it(
        self, db_session, admin, blueprint, monkeypatch
    ):
        service = blueprints_api.get_blueprint_export_service()

        def refuse(**kwargs):
            raise validation_error_quoting_the_config()

        monkeypatch.setattr(service, "export_blueprint", refuse)
        monkeypatch.setattr(blueprints_api, "get_blueprint_export_service", lambda: service)

        with pytest.raises(HTTPException) as raised:
            call_export(blueprint.id, db_session, admin)

        assert raised.value.status_code == 409
        assert SECRET_MARKER not in str(raised.value.detail)
        assert "validation errors" not in str(raised.value.detail)

    def test_an_unexpected_failure_does_not_carry_its_text_either(
        self, db_session, admin, blueprint, monkeypatch
    ):
        service = blueprints_api.get_blueprint_export_service()

        def explode(**kwargs):
            raise OSError(f"/data/proving-ground/{SECRET_MARKER}: no space left on device")

        monkeypatch.setattr(service, "export_blueprint", explode)
        monkeypatch.setattr(blueprints_api, "get_blueprint_export_service", lambda: service)

        with pytest.raises(HTTPException) as raised:
            call_export(blueprint.id, db_session, admin)

        assert raised.value.status_code == 500
        assert SECRET_MARKER not in str(raised.value.detail)

    def test_a_blueprint_that_is_really_missing_is_still_a_404(self, db_session, admin):
        with pytest.raises(HTTPException) as raised:
            blueprints_api.get_export_size(uuid.uuid4(), db_session, admin)

        assert raised.value.status_code == 404
        assert raised.value.detail == "Blueprint not found"

    def test_a_bad_content_id_is_the_callers_400_not_the_servers_500(
        self, db_session, admin, blueprint
    ):
        # This 400 is raised inside the try block, so the catch-all used to swallow it and blame
        # the server for a malformed query string.
        with pytest.raises(HTTPException) as raised:
            call_export(blueprint.id, db_session, admin, content_id="nope")

        assert raised.value.status_code == 400

    def test_a_kubernetes_blueprint_can_still_have_its_size_estimated(
        self, db_session, admin, blueprint
    ):
        # The harm underneath the leak: export answered 404 for every v2 blueprint, about one
        # sitting in front of the user.
        result = blueprints_api.get_export_size(blueprint.id, db_session, admin)
        assert result["blueprint_id"] == str(blueprint.id)
