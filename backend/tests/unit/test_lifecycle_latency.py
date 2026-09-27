"""Starting and stopping a Kubernetes range must not be something the browser waits on.

Both endpoints used to drive the cluster inline. `start` waits for every machine to report
Running -- bounded at ten minutes -- on an anyio threadpool worker, with no timeout at the
browser's end and any proxy in front of the API returning 504 long before the cluster is done.
The UI reported a failure about a range that was in fact coming up, and clicking Start again
cost a second worker.

They now record the intent, hand the work to the deploy worker and answer, as deploy already
did. What is under test here is that answer: the endpoint returns without touching the cluster,
the transition is on the range and in its activity record, and a failure in the worker lands on
the range's status with a reason rather than in a log nobody reads.

The last class is the other half of the same surface: what the deployment panel is told about a
range on this substrate. It used to be told about a Docker-in-Docker container.
"""

import asyncio
import json
import uuid

import pytest

from proving_ground.api import blueprints as blueprints_api
from proving_ground.api import kubernetes_ranges
from proving_ground.api import ranges as ranges_api
from proving_ground.models.blueprint import RangeBlueprint
from proving_ground.models.event_log import EventLog, EventType
from proving_ground.models.range import Range, RangeStatus
from proving_ground.models.user import User, UserRole
from proving_ground.schemas.blueprint import InstanceDeploy
from proving_ground.services import kubernetes_range_service as svc
from proving_ground.tasks import deployment as tasks

from .test_kubernetes_range_service import v2_config
from .test_kubernetes_runtime import FakeKube


@pytest.fixture
def kube(monkeypatch):
    fake = FakeKube()

    async def connect(*args, **kwargs):
        return fake

    monkeypatch.setattr(svc.KubernetesApiClient, "connect", connect)
    monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: True)
    return fake


@pytest.fixture
def db_session(worker_db_session):
    """These tests run worker tasks, which open a second session. See `worker_db_session`."""
    return worker_db_session


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
def dispatched(monkeypatch):
    """Every message the endpoints put on the queue, with the worker never running."""
    sent = []
    monkeypatch.setattr(
        blueprints_api.deploy_range_task, "send", lambda rid: sent.append(("deploy", rid))
    )
    monkeypatch.setattr(tasks.start_range_task, "send", lambda *args: sent.append(("start", *args)))
    monkeypatch.setattr(tasks.stop_range_task, "send", lambda *args: sent.append(("stop", *args)))
    return sent


@pytest.fixture
def k8s_range(db_session, admin, dispatched, kube):
    # No capability: a per-learner one would need an assigned learner before it could be placed,
    # and none of this is about placement.
    bp = RangeBlueprint(
        name="bp", version=1, config=v2_config(capabilities=False), created_by=admin.id
    )
    db_session.add(bp)
    db_session.commit()
    resp = blueprints_api.deploy_instance(
        bp.id, InstanceDeploy(name="k8s", auto_deploy=False), db_session, admin
    )
    rng = db_session.query(Range).filter(Range.id == resp.range_id).first()
    asyncio.run(svc.deploy_range_on_kubernetes(db_session, rng.id))
    rng.status = RangeStatus.RUNNING
    db_session.commit()
    return rng


def _fresh(db):
    """Leave this session's read transaction so the worker's committed writes are visible.

    The worker now commits on a session of its own, as it does in production. This session may
    already have opened a read transaction -- reading `k8s_range.id` after a commit is enough --
    and SQLite holds that snapshot until the transaction ends. `expire_all()` does not end it; it
    re-reads inside the same snapshot and returns the same stale row.
    """
    db.rollback()


def reload(db, range_id):
    """Re-read the range. The worker closes the session it was handed, which detaches the row."""
    _fresh(db)
    return db.query(Range).filter(Range.id == range_id).first()


def events_for(db, range_id, event_type=None):
    _fresh(db)
    query = db.query(EventLog).filter(EventLog.range_id == range_id)
    if event_type:
        query = query.filter(EventLog.event_type == event_type)
    return query.order_by(EventLog.created_at).all()


class TestStartAndStopAnswerWithoutWaiting:
    def test_stop_dispatches_and_does_not_touch_the_cluster(
        self, db_session, admin, k8s_range, kube, dispatched
    ):
        kube.calls.clear()

        result = ranges_api.stop_range(k8s_range.id, db_session, admin)

        assert result.status == RangeStatus.DEPLOYING
        assert dispatched[-1] == ("stop", str(k8s_range.id), str(admin.id))
        # The machines are still running: nothing has been asked of the cluster yet.
        assert kube.vm_running == {"web": True}
        assert kube.calls == []

    def test_start_dispatches_and_does_not_touch_the_cluster(
        self, db_session, admin, k8s_range, kube, dispatched
    ):
        k8s_range.status = RangeStatus.STOPPED
        db_session.commit()
        kube.calls.clear()

        result = ranges_api.start_range(k8s_range.id, db_session, admin)

        assert result.status == RangeStatus.DEPLOYING
        assert dispatched[-1] == ("start", str(k8s_range.id), str(admin.id))
        assert kube.calls == []

    def test_the_request_records_who_asked_and_for_what(
        self, db_session, admin, k8s_range, dispatched
    ):
        ranges_api.stop_range(k8s_range.id, db_session, admin)

        step = events_for(db_session, k8s_range.id, EventType.DEPLOYMENT_STEP)[-1]
        assert step.user_id == admin.id
        assert json.loads(step.extra_data)["operation"] == "stop"
        assert json.loads(step.extra_data)["stage"] == 1


class TestTheWorkerRecordsTheTransition:
    def test_a_stop_sets_stopped_at_and_logs_the_event(
        self, db_session, admin, k8s_range, kube, monkeypatch, task_session_factory
    ):
        monkeypatch.setattr(tasks, "get_session_local", task_session_factory)
        monkeypatch.setattr(tasks.settings, "range_substrate", "kubernetes")

        range_id, admin_id = k8s_range.id, admin.id
        tasks.stop_range_task.fn(str(range_id), str(admin_id))

        stopped_range = reload(db_session, range_id)
        assert stopped_range.status == RangeStatus.STOPPED
        assert stopped_range.stopped_at is not None
        assert kube.vm_running == {"web": False}
        stopped = events_for(db_session, range_id, EventType.RANGE_STOPPED)
        assert len(stopped) == 1 and stopped[0].user_id == admin_id

    def test_a_start_sets_started_at_and_logs_the_event(
        self, db_session, admin, k8s_range, kube, monkeypatch, task_session_factory
    ):
        monkeypatch.setattr(tasks, "get_session_local", task_session_factory)
        monkeypatch.setattr(tasks.settings, "range_substrate", "kubernetes")
        k8s_range.status = RangeStatus.STOPPED
        db_session.commit()

        range_id, admin_id = k8s_range.id, admin.id
        tasks.start_range_task.fn(str(range_id), str(admin_id))

        started_range = reload(db_session, range_id)
        assert started_range.status == RangeStatus.RUNNING
        assert started_range.started_at is not None
        started = events_for(db_session, range_id, EventType.RANGE_STARTED)
        assert len(started) == 1 and started[0].user_id == admin_id

    def test_a_deploy_sets_deployed_at(
        self, db_session, admin, k8s_range, monkeypatch, task_session_factory
    ):
        monkeypatch.setattr(tasks, "get_session_local", task_session_factory)
        monkeypatch.setattr(tasks.settings, "range_substrate", "kubernetes")
        k8s_range.deployed_at = None
        db_session.commit()

        range_id = k8s_range.id
        tasks.deploy_range_task.fn(str(range_id))

        deployed = reload(db_session, range_id)
        assert deployed.deployed_at is not None
        assert deployed.status == RangeStatus.RUNNING

    def test_a_failed_start_leaves_the_range_failed_with_a_reason(
        self, db_session, admin, k8s_range, monkeypatch, task_session_factory
    ):
        monkeypatch.setattr(tasks, "get_session_local", task_session_factory)
        monkeypatch.setattr(tasks.settings, "range_substrate", "kubernetes")

        async def refuse(*args, **kwargs):
            raise TimeoutError("workloads in pg-range-x did not reach Running: web: Provisioning")

        monkeypatch.setattr(svc, "start_range_on_kubernetes", refuse)
        k8s_range.status = RangeStatus.DEPLOYING
        db_session.commit()

        range_id = k8s_range.id
        tasks.start_range_task.fn(str(range_id), str(admin.id))

        failed_range = reload(db_session, range_id)
        assert failed_range.status == RangeStatus.ERROR
        assert "did not reach Running" in failed_range.error_message
        failed = events_for(db_session, range_id, EventType.DEPLOYMENT_FAILED)
        assert failed and "did not reach Running" in failed[-1].message


class TestTheWatcherReportsWhatTheClusterIsDoing:
    """The deploy is one service call, so progress is read off the cluster from beside it."""

    def test_it_walks_the_kubernetes_stages_and_names_each_resource(
        self, db_session, admin, k8s_range, kube, monkeypatch
    ):
        monkeypatch.setattr(tasks, "_PROGRESS_POLL_SECONDS", 0)
        spec = tasks._era_b_spec(db_session, k8s_range)
        placement = svc.placement_for_range(k8s_range, list(spec.capabilities))
        report = tasks._KubernetesProgress(db_session, k8s_range.id, "deploy", tasks.DEPLOY_STAGES)
        db_session.query(EventLog).filter(EventLog.range_id == k8s_range.id).delete()
        db_session.commit()

        # The fixture's range is already realised on the fake cluster, so every milestone is
        # already there to be found and the watcher runs to its end.
        asyncio.run(tasks._watch_deploy(report, spec, placement))

        steps = events_for(db_session, k8s_range.id, EventType.DEPLOYMENT_STEP)
        assert [json.loads(s.extra_data)["stage_name"] for s in steps] == list(tasks.DEPLOY_STAGES)
        created = events_for(db_session, k8s_range.id, EventType.NETWORK_CREATED)
        assert {json.loads(e.extra_data)["resource"] for e in created} == {"dmz", "internal"}
        started = events_for(db_session, k8s_range.id, EventType.VM_STARTED)
        assert [json.loads(e.extra_data)["resource"] for e in started] == ["web"]

    def test_a_watcher_that_fails_does_not_fail_the_work(self):
        async def boom():
            raise RuntimeError("the cluster stopped answering the watcher")

        async def work():
            await asyncio.sleep(0)
            return "deployed"

        assert asyncio.run(tasks._with_progress(work(), boom())) == "deployed"


class TestTheDeploymentPanelSeesKubernetes:
    def build(self, db, range_obj):
        return ranges_api.compute_deployment_status(
            range_obj,
            events_for(db, range_obj.id),
            ranges_api._era_b_blueprint(db, range_obj),
        )

    def test_it_names_the_cluster_resources_and_carries_no_dind_row(
        self, db_session, admin, k8s_range, kube
    ):
        status = self.build(db_session, k8s_range)

        assert status.router is None
        assert [n.name for n in status.networks] == ["dmz", "internal"]
        assert [v.hostname for v in status.vms] == ["web"]
        assert status.summary.total == 3

    def test_the_stages_it_reports_are_kubernetes_stages(
        self, db_session, admin, k8s_range, monkeypatch, task_session_factory
    ):
        monkeypatch.setattr(tasks, "get_session_local", task_session_factory)
        monkeypatch.setattr(tasks.settings, "range_substrate", "kubernetes")
        report = tasks._KubernetesProgress(db_session, k8s_range.id, "deploy", tasks.DEPLOY_STAGES)
        report.step(1, "Creating namespace pg-range-x")
        report.step(2, "Creating network attachments")
        report.resource(EventType.NETWORK_CREATED, "dmz", "Attachment created")
        report.resource(EventType.VM_STARTED, "web", "Machine 'web' is Running")

        status = self.build(db_session, k8s_range)

        assert status.stage_name == "Creating Network Attachments"
        assert (status.current_stage, status.total_stages) == (2, 5)
        assert "DinD" not in json.dumps(status.model_dump())
        assert "Docker" not in json.dumps(status.model_dump())
        by_name = {n.name: n.status for n in status.networks}
        assert by_name == {"dmz": "created", "internal": "pending"}
        assert status.vms[0].status == "running"
        assert status.summary.completed == 2

    def test_a_stop_is_drawn_as_a_stop_and_not_as_the_last_deploy(
        self, db_session, admin, k8s_range
    ):
        deploy = tasks._KubernetesProgress(db_session, k8s_range.id, "deploy", tasks.DEPLOY_STAGES)
        deploy.step(1, "Creating namespace pg-range-x")
        deploy.resource(EventType.VM_STARTED, "web", "Machine 'web' is Running")

        stop = tasks._KubernetesProgress(db_session, k8s_range.id, "stop", tasks.STOP_STAGES)
        stop.step(1, "Stopping machines")

        status = self.build(db_session, k8s_range)

        assert status.stage_name == "Stopping Machines"
        # The networks are not touched by a stop, and the machine is back to pending because the
        # previous deploy's result is not this operation's progress.
        assert status.networks == []
        assert status.summary == status.summary.model_copy(
            update={"total": 1, "completed": 0, "pending": 1}
        )

        stop.resource(EventType.VM_STOPPED, "web", "Machine 'web' stopped")
        assert self.build(db_session, k8s_range).summary.completed == 1

    def test_a_failure_puts_its_reason_on_a_row(self, db_session, admin, k8s_range):
        report = tasks._KubernetesProgress(db_session, k8s_range.id, "deploy", tasks.DEPLOY_STAGES)
        report.step(1, "Creating namespace pg-range-x")
        tasks._era_b_failed(db_session, k8s_range.id, "deploy", RuntimeError("no such image"))

        status = self.build(db_session, k8s_range)

        assert status.summary.failed == 3
        assert "no such image" in status.vms[0].status_detail

    def test_an_era_a_blueprint_on_this_host_keeps_the_era_a_view(
        self, db_session, admin, dispatched, kube
    ):
        bp = RangeBlueprint(
            name="old",
            version=1,
            config={
                "networks": [{"name": "lan", "subnet": "10.1.0.0/24", "gateway": "10.1.0.1"}],
                "vms": [],
            },
            created_by=admin.id,
        )
        db_session.add(bp)
        db_session.commit()
        resp = blueprints_api.deploy_instance(
            bp.id, InstanceDeploy(name="dind", auto_deploy=False), db_session, admin
        )
        range_obj = db_session.query(Range).filter(Range.id == resp.range_id).first()

        status = self.build(db_session, range_obj)

        # Era A rows and the DinD placeholder: this range cannot deploy here, and drawing it as
        # cluster resources would describe something that will never exist.
        assert status.router is not None and status.router.name == "dind-container"
