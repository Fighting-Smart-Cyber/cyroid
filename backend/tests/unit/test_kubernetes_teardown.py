"""The three teardown paths that are not `DELETE /ranges/{id}` -- COSMOS PG-41/PG-58.

`DELETE /ranges/{id}` already destroys the namespace first and refuses to drop the row when the
cluster kept anything. The admin purge, the end of a training event, and deleting a blueprint
instance all predate it and all did the opposite: they dropped the row and left the namespace
running. That is unrecoverable rather than merely wrong -- placement is re-derived from the range
row, so the row is the only thing that still names the namespace, and once it is gone the KubeVirt
VMs, PVCs, network attachments and Helm releases inside have nothing pointing at them.

So each path is tested twice: a clean destroy removes the row, and a destroy the cluster refused
keeps it and says so. Driven against the same fake cluster the rest of the Era B suite uses, with
the Era A branch asserted unchanged alongside.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from proving_ground.api import admin as admin_api
from proving_ground.api import instances as instances_api
from proving_ground.api import kubernetes_ranges
from proving_ground.api import training_events as events_api
from proving_ground.capability import kubernetes_client
from proving_ground.models.blueprint import RangeBlueprint, RangeInstance
from proving_ground.models.event import EventParticipant, EventStatus, TrainingEvent
from proving_ground.models.network import Network
from proving_ground.models.range import Range, RangeStatus
from proving_ground.models.user import User, UserRole
from proving_ground.models.vm import VM
from proving_ground.services import kubernetes_range_service as svc

from .test_kubernetes_range_service import v2_config
from .test_kubernetes_runtime import FakeKube

V1_CONFIG = {
    "networks": [{"name": "lan", "subnet": "10.1.0.0/24", "gateway": "10.1.0.1"}],
    "vms": [{"hostname": "a", "network_name": "lan", "ip_address": "10.1.0.5"}],
}


@pytest.fixture(autouse=True)
def permitted_chart_repository(monkeypatch):
    """Permit the chart the fixture blueprint names, the way an operator would.

    The chart-repository allow list denies everything until an install names something, so a
    blueprint carrying a capability does not parse at all without this.
    """
    from proving_ground.config import get_settings

    monkeypatch.setattr(get_settings(), "chart_repository", "https://stefanprodan.github.io")


@pytest.fixture
def kube(monkeypatch):
    fake = FakeKube()
    fake.vcluster = FakeKube()
    fake.vcluster.helm_status = fake.helm_status

    async def connect(*args, **kwargs):
        return fake

    async def from_kubeconfig_dict(kubeconfig):
        return fake.vcluster

    monkeypatch.setattr(kubernetes_client.KubernetesApiClient, "connect", connect)
    monkeypatch.setattr(svc.KubernetesApiClient, "connect", connect)
    monkeypatch.setattr(svc.KubernetesApiClient, "from_kubeconfig_dict", from_kubeconfig_dict)
    monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: True)
    return fake


@pytest.fixture
def user(db_session):
    def make(role=UserRole.ADMIN):
        tag = uuid.uuid4().hex[:8]
        u = User(
            username=f"u-{tag}",
            email=f"u-{tag}@x.invalid",
            hashed_password="x",
            role=role,
            is_active=True,
            is_approved=True,
        )
        db_session.add(u)
        db_session.flush()
        return u

    return make


@pytest.fixture
def admin(user):
    return user()


@pytest.fixture
def blueprint(db_session, admin):
    def make(config):
        bp = RangeBlueprint(name="bp", version=1, config=config, created_by=admin.id)
        db_session.add(bp)
        db_session.flush()
        return bp

    return make


@pytest.fixture
def k8s_range(db_session, admin, blueprint):
    """A range whose blueprint the cluster can deploy, optionally a learner's inside an event."""

    def make(*, learner=None, event=None, config=None):
        bp = blueprint(config if config is not None else v2_config())
        rng = Range(
            name=f"r-{uuid.uuid4().hex[:6]}",
            status=RangeStatus.DRAFT,
            created_by=admin.id,
            assigned_to_user_id=learner.id if learner else None,
            training_event_id=event.id if event else None,
        )
        db_session.add(rng)
        db_session.flush()
        db_session.add(
            RangeInstance(
                name=rng.name,
                blueprint_id=bp.id,
                blueprint_version=1,
                subnet_offset=0,
                instructor_id=admin.id,
                range_id=rng.id,
            )
        )
        db_session.commit()
        return rng

    return make


def deploy(db_session, rng):
    """Realise the range on the fake cluster so there is a namespace to leave behind."""
    import asyncio

    return asyncio.run(svc.deploy_range_on_kubernetes(db_session, rng.id))["namespace"]


def strand(kube, namespace):
    """Leave a cluster-scoped object behind, which is what `residue()` refuses to shrug off."""
    kube.rbac.append(f"clusterrole/vc-x-v-{namespace}")


# ----------------------------------------------------------------- K8S-006: the admin purge


@pytest.fixture
def docker_host(monkeypatch):
    """Enough of a Docker host for the Era A branch of the admin cleanup to run without one."""
    docker = MagicMock()
    docker.cleanup_range.return_value = {"containers": 0, "networks": 0}
    docker.cleanup_all_proving_ground_resources.return_value = {
        "containers_removed": 0,
        "networks_removed": 0,
        "errors": [],
    }
    dind = MagicMock()

    async def no_containers(*args, **kwargs):
        return []

    dind.list_range_containers = no_containers
    dind.delete_range_container = no_containers
    monkeypatch.setattr(admin_api, "get_docker_service", lambda: docker)
    monkeypatch.setattr(admin_api, "get_dind_service", lambda: dind)
    monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: False)
    return docker


class TestTheAdminPurge:
    def test_purge_destroys_the_namespace_before_dropping_the_row(
        self, db_session, admin, k8s_range, kube
    ):
        rng = k8s_range(learner=admin)
        namespace = deploy(db_session, rng)

        result = admin_api.cleanup_all_resources(
            db_session, admin, admin_api.CleanupRequest(mode=admin_api.CleanupMode.PURGE_RANGES)
        )

        assert namespace not in kube.namespaces
        assert result.namespaces_removed == 1
        assert result.residue == []
        assert db_session.query(Range).filter(Range.id == rng.id).first() is None

    def test_a_range_the_cluster_kept_keeps_its_row_and_is_named(
        self, db_session, admin, k8s_range, kube
    ):
        rng = k8s_range(learner=admin)
        namespace = deploy(db_session, rng)
        strand(kube, namespace)

        result = admin_api.cleanup_all_resources(
            db_session, admin, admin_api.CleanupRequest(mode=admin_api.CleanupMode.PURGE_RANGES)
        )

        assert result.ranges_cleaned == 0
        assert [r.range_id for r in result.residue] == [str(rng.id)]
        assert "residue" in result.residue[0].reason
        assert result.errors and rng.name in result.errors[0]
        assert db_session.query(Range).filter(Range.id == rng.id).first() is not None

    def test_one_stranded_range_does_not_abandon_the_rest(self, db_session, admin, k8s_range, kube):
        kept = k8s_range(learner=admin)
        strand(kube, deploy(db_session, kept))
        clean = k8s_range(learner=admin)
        deploy(db_session, clean)

        result = admin_api.cleanup_all_resources(
            db_session, admin, admin_api.CleanupRequest(mode=admin_api.CleanupMode.PURGE_RANGES)
        )

        assert result.ranges_cleaned == 1
        assert db_session.query(Range).filter(Range.id == clean.id).first() is None
        assert db_session.query(Range).filter(Range.id == kept.id).first() is not None

    def test_an_explicit_purge_is_not_turned_back_into_a_reset(
        self, db_session, admin, k8s_range, kube
    ):
        # `clean_database` defaults to true, and the legacy compatibility block read it after
        # the mode: a request naming purge_ranges came out the other side as a reset.
        rng = k8s_range(learner=admin)
        deploy(db_session, rng)

        admin_api.cleanup_all_resources(
            db_session, admin, admin_api.CleanupRequest(mode=admin_api.CleanupMode.PURGE_RANGES)
        )

        assert db_session.query(Range).filter(Range.id == rng.id).first() is None

    def test_reset_to_draft_destroys_the_namespace_and_keeps_the_definition(
        self, db_session, admin, k8s_range, kube
    ):
        rng = k8s_range(learner=admin)
        namespace = deploy(db_session, rng)

        result = admin_api.cleanup_all_resources(
            db_session, admin, admin_api.CleanupRequest(mode=admin_api.CleanupMode.RESET_TO_DRAFT)
        )

        assert namespace not in kube.namespaces
        assert result.namespaces_removed == 1
        db_session.expire_all()
        assert rng.status == RangeStatus.DRAFT

    def test_the_docker_path_is_unchanged(self, db_session, admin, k8s_range, docker_host):
        # is_kubernetes() is false here, so nothing may reach the cluster helper at all.
        def never(*args, **kwargs):
            raise AssertionError("the Docker path must not destroy on the cluster")

        rng = k8s_range(learner=admin)
        original = kubernetes_ranges.destroy_for_cleanup
        kubernetes_ranges.destroy_for_cleanup = never
        try:
            result = admin_api.cleanup_all_resources(
                db_session,
                admin,
                admin_api.CleanupRequest(mode=admin_api.CleanupMode.PURGE_RANGES),
            )
        finally:
            kubernetes_ranges.destroy_for_cleanup = original

        assert result.ranges_cleaned == 1
        assert result.namespaces_removed == 0
        assert docker_host.cleanup_range.called
        assert db_session.query(Range).filter(Range.id == rng.id).first() is None


# ------------------------------------------------------- K8S-007: completing a training event


@pytest.fixture
def event_with_learners(db_session, admin, user, k8s_range):
    def make(count=2, config=None):
        event = TrainingEvent(
            name=f"ex-{uuid.uuid4().hex[:6]}",
            start_datetime=datetime.now(timezone.utc),
            created_by_id=admin.id,
            status=EventStatus.RUNNING,
        )
        db_session.add(event)
        db_session.flush()
        ranges = []
        for _ in range(count):
            learner = user(UserRole.STUDENT)
            rng = k8s_range(learner=learner, event=event, config=config)
            db_session.add(
                EventParticipant(
                    event_id=event.id,
                    user_id=learner.id,
                    role="student",
                    range_id=rng.id,
                )
            )
            ranges.append(rng)
        db_session.commit()
        return event, ranges

    return make


class TestEndingATrainingEvent:
    def test_completing_an_event_destroys_every_learner_namespace(
        self, db_session, admin, event_with_learners, kube
    ):
        event, ranges = event_with_learners()
        namespaces = [deploy(db_session, r) for r in ranges]

        events_api.complete_event(event.id, db_session, admin, cleanup_ranges=True)

        assert not [n for n in namespaces if n in kube.namespaces]
        assert db_session.query(Range).filter(Range.id.in_([r.id for r in ranges])).count() == 0
        db_session.expire_all()
        assert event.status == EventStatus.COMPLETED

    def test_a_learner_range_the_cluster_kept_keeps_its_row_and_502s(
        self, db_session, admin, event_with_learners, kube
    ):
        event, ranges = event_with_learners()
        kept, clean = ranges
        strand(kube, deploy(db_session, kept))
        deploy(db_session, clean)

        with pytest.raises(HTTPException) as exc:
            events_api.complete_event(event.id, db_session, admin, cleanup_ranges=True)

        assert exc.value.status_code == 502
        assert kept.name in exc.value.detail and "cleanup_ranges=false" in exc.value.detail
        # The one that came down is gone; the one still running keeps the row that names it,
        # and the participant still points at it.
        assert db_session.query(Range).filter(Range.id == clean.id).first() is None
        assert db_session.query(Range).filter(Range.id == kept.id).first() is not None
        participant = (
            db_session.query(EventParticipant).filter(EventParticipant.range_id == kept.id).first()
        )
        assert participant is not None
        db_session.expire_all()
        assert event.status == EventStatus.RUNNING

    def test_cancelling_and_deleting_take_the_same_path(
        self, db_session, admin, event_with_learners, kube
    ):
        event, ranges = event_with_learners(count=1)
        strand(kube, deploy(db_session, ranges[0]))

        with pytest.raises(HTTPException) as cancelled:
            events_api.cancel_event(event.id, db_session, admin, cleanup_ranges=True)
        assert cancelled.value.status_code == 502

        with pytest.raises(HTTPException) as deleted:
            events_api.delete_event(event.id, db_session, admin)
        assert deleted.value.status_code == 502
        assert db_session.query(TrainingEvent).filter(TrainingEvent.id == event.id).first()

    def test_the_docker_path_is_unchanged(
        self, db_session, admin, event_with_learners, monkeypatch
    ):
        monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: False)
        monkeypatch.setattr(
            "proving_ground.services.docker_service.get_docker_service",
            lambda: MagicMock(),
        )
        monkeypatch.setattr(
            "proving_ground.services.dind_service.get_dind_service", lambda: MagicMock()
        )
        event, ranges = event_with_learners()

        events_api.complete_event(event.id, db_session, admin, cleanup_ranges=True)

        assert db_session.query(Range).filter(Range.id.in_([r.id for r in ranges])).count() == 0
        db_session.expire_all()
        assert event.status == EventStatus.COMPLETED


# ------------------------------------------------ K8S-008: deleting a blueprint instance


class TestDeletingABlueprintInstance:
    def test_delete_destroys_before_dropping_the_rows(self, db_session, admin, k8s_range, kube):
        rng = k8s_range(learner=admin)
        namespace = deploy(db_session, rng)
        instance = db_session.query(RangeInstance).filter(RangeInstance.range_id == rng.id).one()

        instances_api.delete_instance(instance.id, db_session, admin)

        assert namespace not in kube.namespaces
        assert db_session.query(Range).filter(Range.id == rng.id).first() is None
        assert db_session.query(RangeInstance).filter(RangeInstance.id == instance.id).first() is (
            None
        )

    def test_a_failed_destroy_keeps_both_rows_and_502s(self, db_session, admin, k8s_range, kube):
        rng = k8s_range(learner=admin)
        strand(kube, deploy(db_session, rng))
        instance = db_session.query(RangeInstance).filter(RangeInstance.range_id == rng.id).one()

        with pytest.raises(HTTPException) as exc:
            instances_api.delete_instance(instance.id, db_session, admin)

        assert exc.value.status_code == 502 and "residue" in exc.value.detail
        assert db_session.query(Range).filter(Range.id == rng.id).first() is not None
        assert (
            db_session.query(RangeInstance).filter(RangeInstance.id == instance.id).first()
            is not None
        )

    def test_a_learner_cannot_delete_an_instructors_instance(
        self, db_session, admin, user, k8s_range, kube
    ):
        rng = k8s_range(learner=admin)
        instance = db_session.query(RangeInstance).filter(RangeInstance.range_id == rng.id).one()

        with pytest.raises(HTTPException) as exc:
            instances_api.delete_instance(instance.id, db_session, user(UserRole.STUDENT))

        assert exc.value.status_code == 403
        assert db_session.query(Range).filter(Range.id == rng.id).first() is not None

    def test_the_docker_path_still_queues_the_teardown_task(
        self, db_session, admin, k8s_range, monkeypatch
    ):
        monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: False)
        sent = []
        monkeypatch.setattr(instances_api.teardown_range_task, "send", lambda rid: sent.append(rid))
        rng = k8s_range(learner=admin)
        instance = db_session.query(RangeInstance).filter(RangeInstance.range_id == rng.id).one()

        instances_api.delete_instance(instance.id, db_session, admin)

        assert sent == [str(rng.id)]
        assert db_session.query(Range).filter(Range.id == rng.id).first() is None


# ------------------------------------------------------- K8S-012: starting an event's labs


@pytest.fixture
def queued(monkeypatch):
    from proving_ground.tasks import deployment

    calls = []
    monkeypatch.setattr(deployment.deploy_range_task, "send", lambda rid: calls.append(rid))
    return calls


@pytest.fixture
def event_for_start(db_session, admin, user, blueprint):
    def make(config, learners=2):
        bp = blueprint(config)
        event = TrainingEvent(
            name=f"class-{uuid.uuid4().hex[:6]}",
            start_datetime=datetime.now(timezone.utc),
            created_by_id=admin.id,
            status=EventStatus.SCHEDULED,
            blueprint_id=bp.id,
        )
        db_session.add(event)
        db_session.flush()
        for _ in range(learners):
            db_session.add(
                EventParticipant(
                    event_id=event.id, user_id=user(UserRole.STUDENT).id, role="student"
                )
            )
        db_session.commit()
        return event

    return make


class TestStartAndDeployLabs:
    def test_a_v2_blueprint_produces_a_range_and_an_instance_per_learner(
        self, db_session, admin, event_for_start, queued, kube
    ):
        # The v1 path built Network and VM rows from the config, which is why this could not
        # provision an Era B blueprint at all; the instance row is what names the blueprint to
        # the deploy, and without it every learner's range landed in ERROR.
        event = event_for_start(v2_config())

        events_api.start_event(event.id, db_session, admin, auto_deploy=True)

        ranges = db_session.query(Range).filter(Range.training_event_id == event.id).all()
        assert len(ranges) == 2
        for rng in ranges:
            assert rng.assigned_to_user_id is not None
            instance = (
                db_session.query(RangeInstance).filter(RangeInstance.range_id == rng.id).one()
            )
            assert instance.blueprint_id == event.blueprint_id
            assert db_session.query(Network).filter(Network.range_id == rng.id).count() == 0
            assert db_session.query(VM).filter(VM.range_id == rng.id).count() == 0
        assert sorted(queued) == sorted(str(r.id) for r in ranges)

    def test_the_deploy_reads_the_blueprint_the_start_linked(
        self, db_session, admin, event_for_start, queued, kube
    ):
        event = event_for_start(v2_config(), learners=1)
        events_api.start_event(event.id, db_session, admin, auto_deploy=True)
        rng = db_session.query(Range).filter(Range.training_event_id == event.id).one()

        namespace = deploy(db_session, rng)

        assert namespace in kube.namespaces
        assert "web" in kube.vm_running

    def test_an_era_a_blueprint_is_refused_with_a_400_and_leaves_nothing_behind(
        self, db_session, admin, event_for_start, queued, kube
    ):
        event = event_for_start(V1_CONFIG)

        with pytest.raises(HTTPException) as exc:
            events_api.start_event(event.id, db_session, admin, auto_deploy=True)

        assert exc.value.status_code == 400 and "Era A" in exc.value.detail
        assert queued == []
        db_session.rollback()
        assert db_session.query(Range).filter(Range.training_event_id == event.id).count() == 0
        assert event.status == EventStatus.SCHEDULED

    def test_an_era_b_blueprint_on_docker_is_refused_rather_than_started_empty(
        self, db_session, admin, event_for_start, queued, monkeypatch
    ):
        # The v2 branch is chosen by the blueprint, not by the substrate, so on a Docker host it
        # would build a range with no networks and no VMs per learner and queue a DinD deploy of
        # it -- an event reporting itself running over nothing.
        monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: False)
        event = event_for_start(v2_config())

        with pytest.raises(HTTPException) as exc:
            events_api.start_event(event.id, db_session, admin, auto_deploy=True)

        assert exc.value.status_code == 400 and "Era B" in exc.value.detail
        assert queued == []
        db_session.rollback()
        assert db_session.query(Range).filter(Range.training_event_id == event.id).count() == 0
        assert event.status == EventStatus.SCHEDULED

    def test_the_v1_path_on_docker_is_unchanged(
        self, db_session, admin, event_for_start, queued, monkeypatch
    ):
        monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: False)
        event = event_for_start(V1_CONFIG, learners=1)

        events_api.start_event(event.id, db_session, admin, auto_deploy=True)

        rng = db_session.query(Range).filter(Range.training_event_id == event.id).one()
        assert db_session.query(Network).filter(Network.range_id == rng.id).count() == 1
        assert queued == [str(rng.id)]
