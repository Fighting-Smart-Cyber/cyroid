"""Era B workloads and consoles -- PG-61.

The websocket endpoint runs under a minimal FastAPI app holding only its router (the real app's
startup dials Redis), with the database fixture and the fake cluster behind it.
"""

import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from proving_ground.api import kubernetes_console as console
from proving_ground.api import kubernetes_ranges
from proving_ground.database import get_db
from proving_ground.models.event import EventParticipant, TrainingEvent
from proving_ground.models.range import Range, RangeStatus
from proving_ground.models.user import User, UserRole
from proving_ground.services import kubernetes_range_service as svc
from proving_ground.api import kubernetes_console as kconsole
from proving_ground.utils.security import (
    WS_TICKET_COOKIE,
    create_access_token,
    create_ws_ticket,
)

from .test_kubernetes_range_service import v2_config
from .test_kubernetes_runtime import FakeKube


def user(db, role=UserRole.STUDENT):
    tag = uuid.uuid4().hex[:8]
    u = User(
        username=f"u-{tag}",
        email=f"u-{tag}@x.invalid",
        hashed_password="x",
        role=role,
        is_active=True,
        is_approved=True,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def kube(monkeypatch):
    fake = FakeKube()

    async def connect(*a, **k):
        return fake

    monkeypatch.setattr(svc.KubernetesApiClient, "connect", connect)
    monkeypatch.setattr(console.KubernetesApiClient, "connect", connect)
    monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: True)
    monkeypatch.setattr(console, "is_kubernetes", lambda: True)
    return fake


@pytest.fixture
def k8s_range(db_session, kube):
    from proving_ground.models.blueprint import RangeBlueprint, RangeInstance

    owner = user(db_session, UserRole.ADMIN)
    learner = user(db_session)
    bp = RangeBlueprint(
        name="bp", version=1, config=v2_config(capabilities=False), created_by=owner.id
    )
    rng = Range(
        name="r", status=RangeStatus.DRAFT, created_by=owner.id, assigned_to_user_id=learner.id
    )
    db_session.add_all([bp, rng])
    db_session.flush()
    db_session.add(
        RangeInstance(
            name="i",
            blueprint_id=bp.id,
            blueprint_version=1,
            subnet_offset=0,
            instructor_id=owner.id,
            range_id=rng.id,
        )
    )
    db_session.commit()
    rng.owner, rng.learner = owner, learner
    return rng


class TestEntitlement:
    def test_owner_learner_and_participant_are_in(self, db_session, k8s_range):
        # (Admin is `user.is_admin`, a role attribute -- trivially the first clause.)
        for u in (k8s_range.owner, k8s_range.learner):
            console.check_range_console_access(k8s_range, u, db_session)
        ev = TrainingEvent(
            name="ex",
            start_datetime=__import__("datetime").datetime.now(),
            created_by_id=k8s_range.owner.id,
        )
        db_session.add(ev)
        db_session.flush()
        participant = user(db_session)
        db_session.add(EventParticipant(event_id=ev.id, user_id=participant.id))
        k8s_range.training_event_id = ev.id
        db_session.flush()
        console.check_range_console_access(k8s_range, participant, db_session)

    def test_a_stranger_is_refused_even_though_the_range_is_untagged(self, db_session, k8s_range):
        # The visibility model's "untagged is public" is not the console's rule.
        with pytest.raises(HTTPException) as exc:
            console.check_range_console_access(k8s_range, user(db_session), db_session)
        assert exc.value.status_code == 403


class TestWorkloads:
    async def test_an_undeployed_range_lists_its_workloads_as_absent(self, db_session, k8s_range):
        r = await console.workloads_of(db_session, k8s_range)
        assert r["substrate"] == "kubernetes"
        assert [(w["name"], w["status"], w["console_available"]) for w in r["workloads"]] == [
            ("web", "absent", False)
        ]

    async def test_a_deployed_range_lists_live_state(self, db_session, k8s_range, kube):
        kube.vmi_interfaces["web"] = [{"name": "net1", "ipAddress": "172.30.10.5"}]
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        r = await console.workloads_of(db_session, k8s_range)
        w = r["workloads"][0]
        assert (w["status"], w["addresses"], w["console_available"], w["os_family"]) == (
            "Running",
            {"net1": "172.30.10.5"},
            True,
            "linux",
        )

    async def test_an_era_a_range_says_so(self, db_session, kube):
        owner = user(db_session, UserRole.ADMIN)
        rng = Range(name="old", status=RangeStatus.DRAFT, created_by=owner.id)
        db_session.add(rng)
        db_session.commit()
        assert await console.workloads_of(db_session, rng) == {"substrate": "dind", "workloads": []}


@pytest.fixture
def client(db_session):
    app = FastAPI()
    app.include_router(console.router)
    app.dependency_overrides[get_db] = lambda: db_session
    # The websocket route calls get_db() directly, not through Depends.
    import proving_ground.api.kubernetes_console as mod

    original = mod.get_db
    mod.get_db = lambda: iter([db_session])
    yield TestClient(app)
    mod.get_db = original


def as_user(client, user_obj, range_id, machine: str) -> None:
    """Give the client the ticket cookie the handshake now carries.

    The session JWT is no longer accepted in this socket's URL -- it used to be, which put a
    full API credential in every access log between the browser and here. The scope is the
    (range, machine) pair, so a ticket for one machine cannot open another.
    """
    client.cookies.set(
        WS_TICKET_COOKIE,
        create_ws_ticket(user_obj.id, kconsole.WS_TICKET_KIND, f"{range_id}/{machine}"),
    )


class TestTheConsoleWebsocket:
    def test_frames_flow_both_ways(self, client, db_session, k8s_range, kube):
        db_session.close = lambda: None  # the endpoint closes its session; keep the fixture's
        kube.vnc_frames = [b"RFB 003.008\n", b"\x00\x01"]
        as_user(client, k8s_range.learner, k8s_range.id, "web")
        with client.websocket_connect(f"/ws/vnc/k8s/{k8s_range.id}/web") as ws:
            assert ws.receive_bytes() == b"RFB 003.008\n"
            ws.send_bytes(b"RFB 003.008\n")
            assert ws.receive_bytes() == b"\x00\x01"
        assert kube.vnc_opened and kube.vnc_opened[0][1] == "web"
        assert kube.vnc_received == [b"RFB 003.008\n"]

    def test_a_stranger_is_closed_with_4003(self, client, db_session, k8s_range, kube):
        db_session.close = lambda: None
        stranger = user(db_session)
        db_session.commit()
        as_user(client, stranger, k8s_range.id, "web")
        with client.websocket_connect(f"/ws/vnc/k8s/{k8s_range.id}/web") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes()
            assert exc.value.code == 4003
        assert not kube.vnc_opened

    def test_no_ticket_is_closed_with_4001(self, client, db_session, k8s_range, kube):
        # No cookie at all. A session token in the query string is not a substitute -- the
        # route stopped reading the query string, which is the point of the change.
        db_session.close = lambda: None
        token = create_access_token(str(k8s_range.owner.id))
        with client.websocket_connect(f"/ws/vnc/k8s/{k8s_range.id}/web?token={token}") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes()
            assert exc.value.code == 4001

    def test_an_unknown_workload_is_closed_with_4004(self, client, db_session, k8s_range, kube):
        db_session.close = lambda: None
        as_user(client, k8s_range.owner, k8s_range.id, "nope")
        with client.websocket_connect(f"/ws/vnc/k8s/{k8s_range.id}/nope") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes()
            assert exc.value.code == 4004
        assert not kube.vnc_opened


class TestMachineSummary:
    """The dashboard tile. Counts what is actually running, in one cluster call."""

    async def test_it_counts_running_instances_across_every_range(
        self, db_session, k8s_range, kube
    ):
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        summary = await console.machine_summary(db_session, k8s_range.owner)
        assert summary == {"substrate": "kubernetes", "running": 1, "total": 1}
        # One request, not one per range.
        assert sum(1 for c in kube.calls if c[0] == "list_cluster_custom_objects") == 1

    async def test_instances_that_are_not_running_are_counted_separately(
        self, db_session, k8s_range, kube
    ):
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        kube.vmi_phase = "Pending"
        summary = await console.machine_summary(db_session, k8s_range.owner)
        assert summary["running"] == 0 and summary["total"] == 1

    async def test_it_selects_by_the_range_label_so_nothing_else_is_counted(
        self, db_session, k8s_range, kube
    ):
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        await console.machine_summary(db_session, k8s_range.owner)
        call = next(c for c in kube.calls if c[0] == "list_cluster_custom_objects")
        assert call[1] == "virtualmachineinstances" and call[2] == "pg.range/id"

    async def test_a_docker_install_is_told_to_count_the_old_way(
        self, db_session, k8s_range, monkeypatch
    ):
        monkeypatch.setattr(console, "is_kubernetes", lambda: False)
        assert await console.machine_summary(db_session, k8s_range.owner) == {
            "substrate": "dind",
            "running": None,
            "total": None,
        }
