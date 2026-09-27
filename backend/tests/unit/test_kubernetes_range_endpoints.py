"""The range endpoints on a host whose `range_substrate` is `kubernetes`.

Called directly, the way `test_catalog_contribution_api` calls its routes: real DB, the fake
cluster behind the service, no app startup. What is under test is that every Era A endpoint
that would otherwise drive DinD hands the request to the cluster instead -- the case that
matters most being delete, which on the DinD path would drop the row and orphan the namespace.
"""

import uuid

import pytest
from fastapi import HTTPException

from proving_ground.api import blueprints as blueprints_api
from proving_ground.api import kubernetes_ranges
from proving_ground.api import ranges as ranges_api
from proving_ground.models.blueprint import RangeBlueprint, RangeInstance
from proving_ground.models.network import Network
from proving_ground.models.range import Range, RangeStatus
from proving_ground.models.user import User, UserRole
from proving_ground.models.vm import VM
from proving_ground.schemas.blueprint import InstanceDeploy
from proving_ground.services import kubernetes_range_service as svc

from .test_kubernetes_range_service import v2_config
from .test_kubernetes_runtime import FakeKube


@pytest.fixture
def kube(monkeypatch):
    fake = FakeKube()

    async def from_kubeconfig(*args, **kwargs):
        return fake

    monkeypatch.setattr(svc.KubernetesApiClient, "connect", from_kubeconfig)
    monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: True)
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
def blueprint(db_session, admin):
    def make(config):
        bp = RangeBlueprint(name="bp", version=1, config=config, created_by=admin.id)
        db_session.add(bp)
        db_session.commit()
        return bp

    return make


def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(blueprints_api.deploy_range_task, "send", lambda rid: calls.append(rid))
    return calls


class TestInstantiatingAnEraBBlueprint:
    def test_makes_a_range_and_an_instance_and_no_era_a_rows(
        self, db_session, admin, blueprint, monkeypatch
    ):
        # Networks and workloads live in the config and are realised on the cluster at deploy
        # time; Network and VM rows are the DinD path's, and the cluster is not told about them.
        calls = sent(monkeypatch)
        bp = blueprint(v2_config())
        resp = blueprints_api.deploy_instance(
            bp.id, InstanceDeploy(name="k8s-1", auto_deploy=True), db_session, admin
        )
        rng = db_session.query(Range).filter(Range.id == resp.range_id).first()
        assert rng.status == RangeStatus.DRAFT
        assert db_session.query(RangeInstance).filter(RangeInstance.range_id == rng.id).count() == 1
        assert db_session.query(Network).filter(Network.range_id == rng.id).count() == 0
        assert db_session.query(VM).filter(VM.range_id == rng.id).count() == 0
        assert calls == [str(rng.id)]

    def test_an_era_a_blueprint_still_takes_the_era_a_path(
        self, db_session, admin, blueprint, monkeypatch
    ):
        sent(monkeypatch)
        bp = blueprint(
            {
                "networks": [{"name": "lan", "subnet": "10.1.0.0/24", "gateway": "10.1.0.1"}],
                "vms": [{"hostname": "a", "network_name": "lan", "ip_address": "10.1.0.5"}],
            }
        )
        resp = blueprints_api.deploy_instance(
            bp.id, InstanceDeploy(name="dind-1", auto_deploy=False), db_session, admin
        )
        # The Era A path builds rows from the config (the VM is skipped for lacking an image
        # source, which is that path's own rule; the network is what proves the branch taken).
        assert db_session.query(Network).filter(Network.range_id == resp.range_id).count() == 1


@pytest.fixture
def k8s_range(db_session, admin, blueprint, monkeypatch, kube):
    sent(monkeypatch)
    bp = blueprint(v2_config(capabilities=False))
    resp = blueprints_api.deploy_instance(
        bp.id, InstanceDeploy(name="k8s", auto_deploy=False), db_session, admin
    )
    return db_session.query(Range).filter(Range.id == resp.range_id).first()


class TestTheRangeEndpointsGoToTheCluster:
    def test_deploy_validates_and_dispatches(self, db_session, admin, k8s_range, monkeypatch):
        calls = []
        from proving_ground.tasks import deployment

        monkeypatch.setattr(deployment.deploy_range_task, "send", lambda rid: calls.append(rid))
        ranges_api.deploy_range(k8s_range.id, db_session, admin)
        assert k8s_range.status == RangeStatus.DEPLOYING
        assert calls == [str(k8s_range.id)]

    def test_deploy_refuses_an_era_a_blueprint_with_a_400(
        self, db_session, admin, blueprint, monkeypatch, kube
    ):
        sent(monkeypatch)
        bp = blueprint({"networks": [], "vms": []})
        resp = blueprints_api.deploy_instance(
            bp.id, InstanceDeploy(name="old", auto_deploy=False), db_session, admin
        )
        with pytest.raises(HTTPException) as exc:
            ranges_api.deploy_range(resp.range_id, db_session, admin)
        assert exc.value.status_code == 400 and "Era A" in exc.value.detail

    def test_stop_and_start_hand_the_work_to_a_worker(
        self, db_session, admin, k8s_range, kube, monkeypatch
    ):
        """The request records the intent and dispatches; the worker touches the cluster.

        Stop and start used to hold the HTTP request open until every machine reached its phase
        -- up to ten minutes, with no client timeout on the other end. What the endpoint owes
        the caller is that the intent is recorded and dispatched, not that it is finished; what
        the worker then does with it is `test_lifecycle_latency.py`.
        """
        import asyncio

        from proving_ground.tasks import deployment

        asyncio.run(svc.deploy_range_on_kubernetes(db_session, k8s_range.id))
        k8s_range.status = RangeStatus.RUNNING
        db_session.commit()
        before = list(kube.calls)

        sent: list[tuple[str, str]] = []
        monkeypatch.setattr(
            deployment.stop_range_task, "send", lambda rid, uid=None: sent.append(("stop", rid))
        )
        monkeypatch.setattr(
            deployment.start_range_task, "send", lambda rid, uid=None: sent.append(("start", rid))
        )

        ranges_api.stop_range(k8s_range.id, db_session, admin)
        assert k8s_range.status == RangeStatus.DEPLOYING
        assert sent == [("stop", str(k8s_range.id))]

        # As the worker would leave it, so the range is startable again.
        k8s_range.status = RangeStatus.STOPPED
        db_session.commit()

        ranges_api.start_range(k8s_range.id, db_session, admin)
        assert k8s_range.status == RangeStatus.DEPLOYING
        assert sent[-1] == ("start", str(k8s_range.id))

        # Neither request went near the cluster: the machines are as the deploy left them.
        assert kube.calls == before
        assert kube.vm_running == {"web": True}

    def test_teardown_destroys_and_returns_to_draft(self, db_session, admin, k8s_range, kube):
        import asyncio

        result = asyncio.run(svc.deploy_range_on_kubernetes(db_session, k8s_range.id))
        k8s_range.status = RangeStatus.RUNNING
        db_session.commit()
        ranges_api.teardown_range(k8s_range.id, db_session, admin)
        assert k8s_range.status == RangeStatus.DRAFT
        assert result["namespace"] not in kube.namespaces

    def test_delete_destroys_before_dropping_the_row(self, db_session, admin, k8s_range, kube):
        import asyncio

        result = asyncio.run(svc.deploy_range_on_kubernetes(db_session, k8s_range.id))
        rid = k8s_range.id
        ranges_api.delete_range(rid, db_session, admin)
        assert result["namespace"] not in kube.namespaces
        assert db_session.query(Range).filter(Range.id == rid).first() is None

    def test_delete_keeps_the_row_when_the_cluster_kept_something(
        self, db_session, admin, k8s_range, kube
    ):
        # The row is the only thing that remembers which namespace this is. Drop it after a
        # failed destroy and the residue is orphaned for good.
        import asyncio

        result = asyncio.run(svc.deploy_range_on_kubernetes(db_session, k8s_range.id))
        kube.rbac.append(f"clusterrole/vc-x-v-{result['namespace']}")
        with pytest.raises(HTTPException) as exc:
            ranges_api.delete_range(k8s_range.id, db_session, admin)
        assert exc.value.status_code == 502 and "residue" in exc.value.detail
        assert db_session.query(Range).filter(Range.id == k8s_range.id).first() is not None
