"""Who may open, list and power a Kubernetes range's machines.

The defect this file exists for: an instructor hid a machine from a learner, the panel said it
was hidden, and the Era B console never consulted the record -- the learner opened it anyway.
A control that reports success and changes nothing is worse than no control at all, so every
Era B path that lists, opens or powers a machine is exercised here against the same visibility
record the Era A path uses.

The second half is the reason it happened: two copies of one access rule. `hidden_machines_for`
is asserted to admit exactly the set `vms.can_access_vm_console` admits, for the same inputs,
so the copies cannot drift apart without this failing.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from proving_ground.api import kubernetes_console as console
from proving_ground.api import ranges as ranges_api
from proving_ground.api import vms as era_a
from proving_ground.models.event import EventParticipant, TrainingEvent
from proving_ground.models.event_log import EventLog, EventType
from proving_ground.models.range import RangeStatus
from proving_ground.models.user import UserAttribute, UserRole
from proving_ground.services import kubernetes_range_service as svc
from proving_ground.utils.security import WS_TICKET_COOKIE, create_ws_ticket
from proving_ground.utils.security import create_access_token

from .test_kubernetes_console import client, k8s_range, kube, user  # noqa: F401

__all__ = ["client", "k8s_range", "kube", "user"]


def with_role(db, role: str):
    account = user(db)
    db.add(UserAttribute(user_id=account.id, attribute_type="role", attribute_value=role))
    db.flush()
    return account


def hide(db, range_obj, *keys):
    range_obj.hidden_vm_ids = list(keys)
    db.flush()


def as_user(client, user_obj, range_id, machine: str = "web") -> None:
    """The ticket cookie the handshake now carries, for one (range, machine) pair.

    These tests are about *entitlement*, not about the credential: minting a ticket is what a
    signed-in user does, and the route still applies the visibility rule afterwards. If it did
    not, minting one here would make every one of these pass.
    """
    client.cookies.set(
        WS_TICKET_COOKIE,
        create_ws_ticket(user_obj.id, console.WS_TICKET_KIND, f"{range_id}/{machine}"),
    )


class TestAHiddenMachineIsActuallyHidden:
    """SEC-033. The record is `Range.hidden_vm_ids`; on Era B its keys are workload names."""

    def test_the_learner_it_is_hidden_from_is_refused_the_console(
        self, client, db_session, k8s_range, kube
    ):
        db_session.close = lambda: None
        hide(db_session, k8s_range, "web")
        db_session.commit()
        as_user(client, k8s_range.learner, k8s_range.id)
        with client.websocket_connect(f"/ws/vnc/k8s/{k8s_range.id}/web") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes()
            assert exc.value.code == 4003
        assert not kube.vnc_opened

    def test_the_owner_still_opens_it(self, client, db_session, k8s_range, kube):
        db_session.close = lambda: None
        hide(db_session, k8s_range, "web")
        db_session.commit()
        kube.vnc_frames = [b"RFB 003.008\n"]
        as_user(client, k8s_range.owner, k8s_range.id)
        with client.websocket_connect(f"/ws/vnc/k8s/{k8s_range.id}/web") as ws:
            assert ws.receive_bytes() == b"RFB 003.008\n"
        assert kube.vnc_opened

    def test_an_admin_still_opens_it(self, client, db_session, k8s_range, kube):
        db_session.close = lambda: None
        hide(db_session, k8s_range, "web")
        admin = with_role(db_session, "admin")
        db_session.commit()
        kube.vnc_frames = [b"RFB 003.008\n"]
        as_user(client, admin, k8s_range.id)
        with client.websocket_connect(f"/ws/vnc/k8s/{k8s_range.id}/web") as ws:
            assert ws.receive_bytes() == b"RFB 003.008\n"
        assert kube.vnc_opened

    def test_hiding_nothing_leaves_the_console_open(self, client, db_session, k8s_range, kube):
        db_session.close = lambda: None
        kube.vnc_frames = [b"RFB 003.008\n"]
        as_user(client, k8s_range.learner, k8s_range.id)
        with client.websocket_connect(f"/ws/vnc/k8s/{k8s_range.id}/web") as ws:
            assert ws.receive_bytes() == b"RFB 003.008\n"

    async def test_a_hidden_machine_is_left_out_of_the_listing(self, db_session, k8s_range):
        """Listed and then refused is a broken console; not listed is a hidden machine."""
        hide(db_session, k8s_range, "web")
        db_session.commit()
        assert await console.workloads_of(db_session, k8s_range, viewer=k8s_range.learner) == {
            "substrate": "kubernetes",
            "namespace": svc.placement_for_range(k8s_range, []).namespace,
            "apps": [],
            "workloads": [],
        }
        listed = await console.workloads_of(db_session, k8s_range, viewer=k8s_range.owner)
        assert [w["name"] for w in listed["workloads"]] == ["web"]


class TestOneRuleNotTwo:
    """AUTHZ-063. Era A answers "is this VM hidden from you"; Era B asks the same question.

    `can_access_vm_console` reads only `vm.id` and `vm.range_id`, so a stand-in carrying those
    two is the same input -- and lets the comparison run without a VM row Era B would never
    create anyway.
    """

    def equivalent(self, db, range_obj, account, key):
        era_b_hidden = key in console.hidden_machines_for(range_obj, account, db)
        vm = SimpleNamespace(id=key, range_id=range_obj.id)
        era_a_hidden = not era_a.can_access_vm_console(vm, account, db)
        assert era_a_hidden == era_b_hidden, (
            f"the two copies of the rule disagree for {account.username}: "
            f"Era A hidden={era_a_hidden}, Era B hidden={era_b_hidden}"
        )
        return era_b_hidden

    def test_they_agree_on_every_kind_of_account(self, db_session, k8s_range):
        key = str(uuid.uuid4())
        hide(db_session, k8s_range, key)
        accounts = {
            "learner": k8s_range.learner,
            "owner": k8s_range.owner,
            "stranger": user(db_session),
            "engineer": with_role(db_session, "engineer"),
            "evaluator": with_role(db_session, "evaluator"),
            "admin": with_role(db_session, "admin"),
        }
        db_session.commit()
        hidden = {
            name: self.equivalent(db_session, k8s_range, a, key) for name, a in accounts.items()
        }
        # The assignment is what the range-level record binds to; a role is not a relationship
        # to someone else's exercise, but it does mean nothing is hidden from you.
        assert hidden["learner"] is True
        assert all(not h for name, h in hidden.items() if name != "learner")

    def test_they_agree_when_the_record_is_the_event_participant_s(self, db_session, k8s_range):
        key = str(uuid.uuid4())
        event = TrainingEvent(
            name="ex",
            start_datetime=__import__("datetime").datetime.now(),
            created_by_id=k8s_range.owner.id,
        )
        db_session.add(event)
        db_session.flush()
        participant = user(db_session)
        db_session.add(
            EventParticipant(event_id=event.id, user_id=participant.id, hidden_vm_ids=[key])
        )
        k8s_range.training_event_id = event.id
        db_session.commit()
        assert self.equivalent(db_session, k8s_range, participant, key) is True
        assert self.equivalent(db_session, k8s_range, participant, "other") is False

    def test_a_machine_nobody_hid_is_hidden_from_nobody(self, db_session, k8s_range):
        assert self.equivalent(db_session, k8s_range, k8s_range.learner, "web") is False


class TestPerMachinePower:
    """K8S-043. Powering one machine is a change to the range, not a use of it."""

    async def power(self, db, range_obj, account, action, machine="web"):
        return await console.set_machine_power(
            range_obj.id, machine, console.MachinePowerRequest(action=action), db, account
        )

    async def test_a_learner_may_not_power_a_machine_they_can_console_into(
        self, db_session, k8s_range, kube
    ):
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        with pytest.raises(HTTPException) as exc:
            await self.power(db_session, k8s_range, k8s_range.learner, "stop")
        assert exc.value.status_code == 403
        assert kube.vm_running == {"web": True}

    async def test_the_owner_may(self, db_session, k8s_range, kube):
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        result = await self.power(db_session, k8s_range, k8s_range.owner, "stop")
        assert result["machine"] == "web" and result["changed"] is True
        assert kube.vm_running == {"web": False}

    async def test_stopping_what_is_already_stopped_says_so_rather_than_claiming_a_change(
        self, db_session, k8s_range, kube
    ):
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        await self.power(db_session, k8s_range, k8s_range.owner, "stop")
        assert (await self.power(db_session, k8s_range, k8s_range.owner, "stop"))[
            "changed"
        ] is False

    async def test_stopping_every_machine_stops_the_range(self, db_session, k8s_range, kube):
        """The defect this closes: the range kept claiming `running` with nothing running in it,
        and the learner portal then admitted a student to an empty lab."""
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        k8s_range.status = RangeStatus.RUNNING
        db_session.commit()

        result = await self.power(db_session, k8s_range, k8s_range.owner, "stop")

        assert kube.vm_running == {"web": False}
        assert result["range_status"] == "stopped"
        db_session.refresh(k8s_range)
        assert k8s_range.status == RangeStatus.STOPPED

    async def test_starting_one_machine_brings_a_stopped_range_back(
        self, db_session, k8s_range, kube
    ):
        """The mirror, which is worse: the Workloads panel is gated on the range's status, so a
        machine started on a stopped range was one the operator could no longer see."""
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        k8s_range.status = RangeStatus.RUNNING
        db_session.commit()
        await self.power(db_session, k8s_range, k8s_range.owner, "stop")
        db_session.refresh(k8s_range)
        assert k8s_range.status == RangeStatus.STOPPED

        result = await self.power(db_session, k8s_range, k8s_range.owner, "start")

        assert result["range_status"] == "running"
        db_session.refresh(k8s_range)
        assert k8s_range.status == RangeStatus.RUNNING

    async def test_a_per_machine_power_change_is_in_the_activity_log(
        self, db_session, k8s_range, kube
    ):
        """Era B wrote no event at all, so 'who turned this machine off' had no answer."""
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        await self.power(db_session, k8s_range, k8s_range.owner, "stop")

        logged = (
            db_session.query(EventLog)
            .filter(
                EventLog.range_id == k8s_range.id,
                EventLog.event_type == EventType.VM_STOPPED,
            )
            .all()
        )
        assert len(logged) == 1
        assert "web" in logged[0].message
        assert logged[0].user_id == k8s_range.owner.id

    async def test_starting_one_machine_leaves_the_rest_of_the_range_alone(
        self, db_session, k8s_range, kube
    ):
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        await self.power(db_session, k8s_range, k8s_range.owner, "stop")
        await self.power(db_session, k8s_range, k8s_range.owner, "start")
        assert kube.vm_running == {"web": True}
        # Never the namespace-wide switch: that is the whole point of a per-machine control.
        assert not [c for c in kube.calls if c[0] == "set_virtual_machines_running"]

    async def test_restart_deletes_the_instance_so_the_controller_rebuilds_it(
        self, db_session, k8s_range, kube
    ):
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        assert (await self.power(db_session, k8s_range, k8s_range.owner, "restart"))["changed"]
        assert ("delete_custom_object", "virtualmachineinstances", "web") in kube.calls
        assert kube.vm_running == {"web": True}

    async def test_restarting_a_stopped_machine_is_refused_with_the_reason(
        self, db_session, k8s_range, kube
    ):
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        await self.power(db_session, k8s_range, k8s_range.owner, "stop")
        with pytest.raises(HTTPException) as exc:
            await self.power(db_session, k8s_range, k8s_range.owner, "restart")
        assert exc.value.status_code == 409 and "start it" in exc.value.detail

    async def test_a_machine_this_range_does_not_declare_is_a_404(
        self, db_session, k8s_range, kube
    ):
        await svc.deploy_range_on_kubernetes(db_session, k8s_range.id)
        with pytest.raises(HTTPException) as exc:
            await self.power(db_session, k8s_range, k8s_range.owner, "stop", machine="nope")
        assert exc.value.status_code == 404

    async def test_an_undeployed_range_says_so_instead_of_failing_silently(
        self, db_session, k8s_range, kube
    ):
        with pytest.raises(HTTPException) as exc:
            await self.power(db_session, k8s_range, k8s_range.owner, "stop")
        assert exc.value.status_code == 409 and "deploy the range" in exc.value.detail


class TestTheVisibilityPanelHasSomethingToShow:
    """The panel returned an empty list on Era B, which read as "nothing to hide"."""

    def test_it_lists_the_blueprint_s_machines_with_live_status(self, db_session, k8s_range):
        assert console.machine_visibility_view(db_session, k8s_range) == [
            {"id": "web", "hostname": "web", "status": "absent"}
        ]

    def test_an_unreachable_cluster_costs_status_not_the_panel(
        self, db_session, k8s_range, monkeypatch
    ):
        async def refuse(*a, **k):
            raise RuntimeError("no route to cluster")

        monkeypatch.setattr(console.KubernetesApiClient, "connect", refuse)
        assert console.machine_visibility_view(db_session, k8s_range) == [
            {"id": "web", "hostname": "web", "status": "unknown"}
        ]

    def test_a_docker_install_is_left_to_its_vm_rows(self, db_session, k8s_range, monkeypatch):
        monkeypatch.setattr(console, "is_kubernetes", lambda: False)
        assert console.machine_visibility_view(db_session, k8s_range) is None
        assert console.machine_keys_of(db_session, k8s_range) is None


class TestThePanelWritesWhatTheConsoleReads:
    """The panel and the enforcement are only one control if they agree on the key.

    Everything above tests one half or the other. This is the round trip, because a key the
    panel accepts and stores but the console never matches is the original defect wearing a
    green toast -- the instructor is told the machine is hidden and it is not.
    """

    def save(self, db, range_obj, account, keys):
        return ranges_api.update_range_vm_visibility(
            range_obj.id, ranges_api.RangeVMVisibilityUpdate(hidden_vm_ids=keys), db, account
        )

    def test_what_the_panel_saves_is_what_the_console_refuses(self, db_session, k8s_range):
        saved = self.save(db_session, k8s_range, k8s_range.owner, ["web"])
        assert saved.hidden_vm_ids == ["web"]
        assert [(m.id, m.is_hidden) for m in saved.vms] == [("web", True)]
        assert console.hidden_machines_for(k8s_range, k8s_range.learner, db_session) == {"web"}

    def test_a_key_naming_no_machine_of_the_range_is_refused_rather_than_stored(
        self, db_session, k8s_range
    ):
        # Storing it would be the whole finding again: accepted, reported saved, matched by
        # nothing. A VM row's id is exactly the shape of key that would arrive here by mistake.
        with pytest.raises(HTTPException) as exc:
            self.save(db_session, k8s_range, k8s_range.owner, [str(uuid.uuid4())])
        assert exc.value.status_code == 400
        assert "machine" in exc.value.detail
        assert not (k8s_range.hidden_vm_ids or [])

    def test_showing_a_machine_again_clears_it(self, db_session, k8s_range):
        self.save(db_session, k8s_range, k8s_range.owner, ["web"])
        assert self.save(db_session, k8s_range, k8s_range.owner, []).hidden_vm_ids == []
        assert console.hidden_machines_for(k8s_range, k8s_range.learner, db_session) == set()

    def test_a_learner_may_not_hide_machines_from_themselves(self, db_session, k8s_range):
        with pytest.raises(HTTPException) as exc:
            self.save(db_session, k8s_range, k8s_range.learner, ["web"])
        assert exc.value.status_code == 403


class TestTheRangeListCounts:
    """UI-057. Every Kubernetes range advertised "0 networks / 0 VMs"."""

    def test_a_kubernetes_range_counts_what_its_blueprint_declares(self, db_session, k8s_range):
        assert console.range_machine_counts(db_session, [k8s_range.id]) == {k8s_range.id: (2, 1)}

    def test_it_is_one_query_for_the_whole_page(self, db_session, k8s_range):
        # A range with no blueprint contributes no entry rather than a wrong zero.
        assert console.range_machine_counts(db_session, [uuid.uuid4()]) == {}
        assert console.range_machine_counts(db_session, []) == {}
