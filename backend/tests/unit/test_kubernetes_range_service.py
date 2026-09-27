"""`deploy_range_on_kubernetes` and `destroy_range_on_kubernetes` -- the Era B seam.

Driven against the fake cluster so the *order* is what is under test: a range must have its
namespace and isolation floor before its networks, its networks before the workloads that attach
to them, and everything applied before anything is waited on. The real cluster is exercised by
`scripts/smoke-range-deploy-kubernetes.py` on pg-devtest.
"""

import uuid

import pytest

from proving_ground.capability import kubernetes_client
from proving_ground.models.blueprint import RangeBlueprint, RangeInstance
from proving_ground.models.range import Range, RangeStatus
from proving_ground.models.user import User, UserRole
from proving_ground.services import kubernetes_range_service as svc

from .test_kubernetes_runtime import FakeKube

BUSYBOX = "docker.io/library/busybox@sha256:" + "a" * 64
CIRROS = "quay.io/kubevirt/cirros-container-disk-demo@sha256:" + "c" * 64


def v2_config(*, workloads=True, capabilities=True):
    config = {
        "schemaVersion": 2,
        "networks": [
            {"name": "dmz", "subnet": "172.30.10.0/24", "gateway": "172.30.10.1"},
            {"name": "internal", "subnet": "172.30.20.0/24"},
        ],
        "workloads": [],
        "capabilities": [],
    }
    if workloads:
        config["workloads"] = [
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
        ]
    if capabilities:
        config["capabilities"] = [
            {
                "name": "podinfo",
                "version": "1.0.0",
                "scope": "per-learner",
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
        ]
    return config


@pytest.fixture
def db_session(worker_db_session):
    """The task-dispatch tests run worker tasks, which open a second session for the progress
    reporter. See `worker_db_session` in conftest for why that needs a real connection each."""
    return worker_db_session


@pytest.fixture
def kube(monkeypatch):
    fake = FakeKube()
    # A second fake stands in for the vcluster: what `from_kubeconfig_dict` hands back once the
    # vcluster's kubeconfig has been read. Hooks must land there, charts on the host.
    fake.vcluster = FakeKube()
    fake.vcluster.helm_status = fake.helm_status

    async def connect(*args, **kwargs):
        return fake

    async def from_kubeconfig_dict(kubeconfig):
        fake.vcluster.kubeconfig = kubeconfig
        return fake.vcluster

    monkeypatch.setattr(kubernetes_client.KubernetesApiClient, "connect", connect)
    monkeypatch.setattr(svc.KubernetesApiClient, "connect", connect)
    monkeypatch.setattr(svc.KubernetesApiClient, "from_kubeconfig_dict", from_kubeconfig_dict)
    return fake


@pytest.fixture
def range_row(db_session):
    def make(config, *, assigned=True, event=False):
        suffix = uuid.uuid4().hex[:8]
        instructor = User(
            username=f"i-{suffix}",
            email=f"i-{suffix}@x.invalid",
            hashed_password="x",
            role=UserRole.ADMIN,
            is_active=True,
            is_approved=True,
        )
        learner = User(
            username=f"l-{suffix}",
            email=f"l-{suffix}@x.invalid",
            hashed_password="x",
            is_active=True,
            is_approved=True,
        )
        db_session.add_all([instructor, learner])
        db_session.flush()
        event_id = None
        if event:
            from datetime import datetime, timezone

            from proving_ground.models.event import TrainingEvent

            ev = TrainingEvent(
                name=f"ex-{suffix}",
                start_datetime=datetime.now(timezone.utc),
                created_by_id=instructor.id,
            )
            db_session.add(ev)
            db_session.flush()
            event_id = ev.id
        blueprint = RangeBlueprint(name="bp", version=1, config=config, created_by=instructor.id)
        rng = Range(
            name="r",
            status=RangeStatus.DRAFT,
            created_by=instructor.id,
            assigned_to_user_id=learner.id if assigned else None,
            training_event_id=event_id,
        )
        db_session.add_all([blueprint, rng])
        db_session.flush()
        db_session.add(
            RangeInstance(
                name="inst",
                blueprint_id=blueprint.id,
                blueprint_version=1,
                subnet_offset=0,
                instructor_id=instructor.id,
                range_id=rng.id,
            )
        )
        db_session.commit()
        return rng

    return make


def call_order(kube):
    return [c[0] if c[0] != "apply_custom_object" else c[1] for c in kube.calls]


class TestDeployRealisesTheWholeRange:
    async def test_networks_then_workloads_then_capabilities(self, db_session, kube, range_row):
        rng = range_row(v2_config())
        kube.vmi_interfaces["web"] = [{"name": "net1", "ipAddress": "172.30.10.5"}]

        result = await svc.deploy_range_on_kubernetes(db_session, rng.id)

        order = call_order(kube)
        first = {
            step: order.index(step)
            for step in (
                "ensure_namespace",
                "apply_network_policy",
                "network-attachment-definitions",
                "virtualmachines",
                "helmrepositories",
                "helmreleases",
                "run_job",
            )
        }
        assert (
            first["ensure_namespace"]
            < first["apply_network_policy"]
            < first["network-attachment-definitions"]
            < first["virtualmachines"]
            < first["helmrepositories"]
            < first["helmreleases"]
            < first["run_job"]
        )
        assert result["networks"] == ["pg-net-dmz", "pg-net-internal"]
        assert result["workloads"] == {
            "web": {"status": "Running", "addresses": {"net1": "172.30.10.5"}}
        }
        assert result["capabilities"] == ["podinfo"]
        assert kube.closed

    async def test_the_range_id_labels_the_namespace_and_the_vms(self, db_session, kube, range_row):
        rng = range_row(v2_config())
        result = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        vm = kube.objects[("virtualmachines", result["namespace"], "web")]
        assert vm["metadata"]["labels"]["pg.range/id"] == str(rng.id)

    async def test_a_range_with_no_workloads_still_deploys(self, db_session, kube, range_row):
        # Capability-only ranges are legitimate: the SD-1 demo is one.
        rng = range_row(v2_config(workloads=False))
        result = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        assert result["workloads"] == {}
        assert not [c for c in kube.calls if c[:2] == ("apply_custom_object", "virtualmachines")]

    async def test_a_vm_that_fails_fails_the_deploy(self, db_session, kube, range_row):
        # Not "collected and the range still reports success" -- that is the Era A shape the
        # handoff names as a defect, and it is not being ported.
        rng = range_row(v2_config())
        kube.vm_statuses["web"] = ["ErrImagePull"]
        with pytest.raises(RuntimeError, match="ErrImagePull"):
            await svc.deploy_range_on_kubernetes(db_session, rng.id)
        assert kube.closed

    async def test_an_era_a_blueprint_is_refused_before_touching_the_cluster(
        self, db_session, kube, range_row
    ):
        rng = range_row({"networks": [], "vms": []})
        with pytest.raises(ValueError, match="Era A"):
            await svc.deploy_range_on_kubernetes(db_session, rng.id)
        assert not kube.calls


class TestDestroyLeavesNothing:
    async def test_destroy_removes_the_namespace_and_reports_clean(
        self, db_session, kube, range_row
    ):
        rng = range_row(v2_config())
        deployed = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        assert deployed["namespace"] in kube.namespaces

        result = await svc.destroy_range_on_kubernetes(db_session, rng.id)

        assert result["namespace"] == deployed["namespace"]
        assert deployed["namespace"] not in kube.namespaces
        assert result["clean"] is True
        assert result["residue"] == "clean"

    async def test_residue_is_a_failure_not_a_footnote(self, db_session, kube, range_row):
        rng = range_row(v2_config())
        deployed = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        kube.rbac.append(f"clusterrole/vc-vcluster-v-{deployed['namespace']}")
        with pytest.raises(RuntimeError, match="left residue.*clusterrole"):
            await svc.destroy_range_on_kubernetes(db_session, rng.id)

    async def test_destroying_a_range_that_was_never_deployed_is_clean(
        self, db_session, kube, range_row
    ):
        # Teardown re-derives placement rather than reading a stored name, so it works on a
        # range whose deploy never ran, or failed before the namespace existed.
        rng = range_row(v2_config())
        result = await svc.destroy_range_on_kubernetes(db_session, rng.id)
        assert result["clean"] is True


def team_exercise_config():
    config = v2_config()
    config["capabilities"][0]["scope"] = "shared"
    return config


class TestATeamExerciseGoesIntoAVcluster:
    """Event, nobody assigned: the range is the isolation boundary. The vcluster is created in the
    placement's namespace; VMs and networks stay in that host namespace (ADR-0002, 2026-09-15);
    the capability is installed *into* the vcluster and its hooks run *inside* it."""

    async def test_the_vcluster_is_created_and_waited_for(self, db_session, kube, range_row):
        rng = range_row(team_exercise_config(), assigned=False, event=True)
        result = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        assert result["isolation"] == "vcluster" and result["vcluster"]
        assert ("helmreleases", result["namespace"], "vcluster") in kube.objects
        assert kube.vcluster.kubeconfig["clusters"][0]["cluster"]["server"].startswith(
            f"https://vcluster.{result['namespace']}"
        )

    async def test_the_chart_is_reconciled_by_the_host_and_deploys_into_the_vcluster(
        self, db_session, kube, range_row
    ):
        rng = range_row(team_exercise_config(), assigned=False, event=True)
        result = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        release = kube.objects[("helmreleases", result["namespace"], "podinfo")]
        assert release["spec"]["kubeConfig"]["secretRef"]["name"] == "vc-vcluster"
        assert ("helmreleases", result["namespace"], "podinfo") not in kube.vcluster.objects

    async def test_hooks_run_inside_the_vcluster(self, db_session, kube, range_row):
        rng = range_row(team_exercise_config(), assigned=False, event=True)
        await svc.deploy_range_on_kubernetes(db_session, rng.id)
        assert any(c[0] == "run_job" for c in kube.vcluster.calls)
        assert not any(c[0] == "run_job" for c in kube.calls)

    async def test_vms_and_networks_stay_in_the_host_namespace(self, db_session, kube, range_row):
        rng = range_row(team_exercise_config(), assigned=False, event=True)
        result = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        assert ("virtualmachines", result["namespace"], "web") in kube.objects
        assert not any(k[0] == "virtualmachines" for k in kube.vcluster.objects)

    async def test_destroy_uninstalls_the_capability_before_the_vcluster(
        self, db_session, kube, range_row
    ):
        # A release still uninstalling from a vcluster that has gone is stranded on its
        # finalizer, and the namespace with it.
        rng = range_row(team_exercise_config(), assigned=False, event=True)
        await svc.deploy_range_on_kubernetes(db_session, rng.id)
        kube.calls.clear()
        result = await svc.destroy_range_on_kubernetes(db_session, rng.id)
        deleted = [
            c[2] for c in kube.calls if c[0] == "delete_custom_object" and c[1] == "helmreleases"
        ]
        assert deleted.index("podinfo") < deleted.index("vcluster")
        assert [c[0] for c in kube.calls].index("delete_namespace") > [
            c[0] for c in kube.calls
        ].index("delete_custom_object")
        assert result["clean"] is True

    async def test_destroy_of_a_range_whose_vcluster_never_came_up_does_not_wait_on_it(
        self, db_session, kube, range_row
    ):
        rng = range_row(team_exercise_config(), assigned=False, event=True)
        result = await svc.destroy_range_on_kubernetes(db_session, rng.id)
        assert result["clean"] is True
        assert not any(c[0] == "run_job" for c in kube.vcluster.calls)


class TestStopAndStart:
    async def test_stop_leaves_the_namespace_and_reports_what_it_stopped(
        self, db_session, kube, range_row
    ):
        rng = range_row(v2_config())
        deployed = await svc.deploy_range_on_kubernetes(db_session, rng.id)
        result = await svc.stop_range_on_kubernetes(db_session, rng.id)
        assert result["namespace"] == deployed["namespace"]
        assert result["stopped"] == 2 + 1  # the fake's two scalable workloads, plus the VM
        assert deployed["namespace"] in kube.namespaces
        assert kube.vm_running == {"web": False}

    async def test_start_waits_for_the_vms_and_returns_their_addresses(
        self, db_session, kube, range_row
    ):
        rng = range_row(v2_config())
        await svc.deploy_range_on_kubernetes(db_session, rng.id)
        await svc.stop_range_on_kubernetes(db_session, rng.id)
        kube.vmi_interfaces["web"] = [{"name": "net1", "ipAddress": "172.30.10.5"}]
        result = await svc.start_range_on_kubernetes(db_session, rng.id)
        assert kube.vm_running == {"web": True}
        assert result["workloads"] == {
            "web": {"status": "Running", "addresses": {"net1": "172.30.10.5"}}
        }


class TestValidation:
    def test_a_v2_blueprint_with_a_learner_validates(self, db_session, range_row):
        rng = range_row(v2_config())
        placement = svc.validate_range_for_kubernetes(db_session, rng.id)
        assert placement.namespace.startswith("pg-")

    def test_an_era_a_blueprint_is_refused_up_front(self, db_session, range_row):
        rng = range_row({"networks": [], "vms": []})
        with pytest.raises(ValueError, match="Era A"):
            svc.validate_range_for_kubernetes(db_session, rng.id)

    def test_a_per_learner_range_with_nobody_assigned_is_refused_up_front(
        self, db_session, range_row
    ):
        # The deploy endpoint should say this before dispatching, not the worker ten seconds
        # later in an event nobody is watching.
        rng = range_row(v2_config(), assigned=False)
        with pytest.raises(ValueError, match="training event"):
            svc.validate_range_for_kubernetes(db_session, rng.id)


class TestTheTaskDispatch:
    """The dramatiq bodies, called directly as the actors call them."""

    @pytest.fixture
    def era_b(self, monkeypatch, db_session, kube, task_session_factory):
        from proving_ground.tasks import deployment

        monkeypatch.setattr(deployment.settings, "range_substrate", "kubernetes")
        # The task opens and closes its own session; hand it the fixture's, and make the close
        # a no-op so the test can still read the row afterwards.
        monkeypatch.setattr(deployment, "get_session_local", task_session_factory)
        monkeypatch.setattr(db_session, "close", lambda: None)
        return deployment

    def test_deploy_then_teardown_returns_the_range_to_draft(self, era_b, db_session, range_row):
        rng = range_row(v2_config())

        era_b.deploy_range_task.fn(str(rng.id))
        db_session.expire_all()
        assert rng.status == RangeStatus.RUNNING

        era_b.teardown_range_task.fn(str(rng.id))
        db_session.expire_all()
        assert rng.status == RangeStatus.DRAFT
        assert rng.error_message is None

    def test_a_teardown_that_leaves_residue_is_an_error_not_a_draft(
        self, era_b, db_session, range_row, kube
    ):
        rng = range_row(v2_config())
        era_b.deploy_range_task.fn(str(rng.id))
        kube.pvs.append("pv/pvc-stranded-in-pg-" + "x")
        kube.list_persistent_volumes_matching = _always(kube.pvs)

        era_b.teardown_range_task.fn(str(rng.id))
        db_session.expire_all()
        assert rng.status == RangeStatus.ERROR
        assert "left residue" in (rng.error_message or "")

    def test_a_failed_deploy_records_the_reason(self, era_b, db_session, range_row, kube):
        rng = range_row(v2_config())
        kube.vm_statuses["web"] = ["DataVolumeError"]
        era_b.deploy_range_task.fn(str(rng.id))
        db_session.expire_all()
        assert rng.status == RangeStatus.ERROR
        assert "DataVolumeError" in rng.error_message


def _always(value):
    async def _f(needle):
        return list(value)

    return _f
