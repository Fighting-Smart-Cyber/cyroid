"""Era B workloads and their consoles -- COSMOS PG-61 (RNET-4).

An Era B range has no VM rows; its workloads are the blueprint's `WorkloadSpec`s, realised as
KubeVirt `VirtualMachine`s in the range's namespace. Two things the UI needs from that:

    GET  /ranges/{id}/workloads                     what is there and what it is doing, live
    WS   /ws/vnc/k8s/{id}/{workload}                the VM's VNC framebuffer, for noVNC
    POST /ranges/{id}/workloads/{workload}/power    start, stop or restart one machine

The console is the interesting one. A container console came off a service the VM image ran
(KasmVNC in dockur) through a Traefik route into the range's network. A KubeVirt VM's VNC comes
off the launcher pod through the API server's `virtualmachineinstances/{name}/vnc` subresource:
the API pod opens it with its ServiceAccount and pipes bytes to the browser. No route into the
range network, no VNC password, no port -- entitlement is the same rule the container console
uses (admin, owner, assigned learner, event participant), checked here before a byte moves.

A browser cannot set headers on a websocket, so this socket took the session JWT as `?token=`.
A URL is written to every proxy and ingress log it passes, kept in the browser's history, and
handed on in a Referer, so that left a credential good for the whole API in all three places.
The fix is the ws ticket in `utils/security.py`, the same one the Era A sockets use: an
authenticated request exchanges its session for a ticket naming ONE socket, delivered as a
cookie whose path is that socket's own path, so the handshake sends it by itself and the URL
carries nothing secret at all.

    POST /ranges/{id}/workloads/{name}/console-ticket   the session, exchanged
    WS   /ws/vnc/k8s/{id}/{name}                        no credential in the URL

The scope is this module's own -- one kind, and a (range, machine) pair for the resource --
because `api/websocket.py`'s minting route names its sockets by a single resource id and this
one is named by two. `typ` keeps the credentials apart in both directions, exactly as
`decode_access_token` refuses anything whose `typ` is not `access`.

Console entitlement and machine visibility both live here rather than in the endpoints, because
they are one rule asked in three places (the listing, the console, the visibility panel) and
CLAUDE.md records what a second copy of an access rule costs: it gets fixed once and stays broken
in the other place. `api/vms.py` still carries the Era A copy; see the note on
`hidden_machines_for`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, Sequence
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from starlette.websockets import WebSocketState

from proving_ground.api.deps import (
    CurrentUser,
    DBSession,
    check_resource_control,
    filter_by_visibility,
)
from proving_ground.api.kubernetes_ranges import is_kubernetes
from proving_ground.capability.blueprint import RangeSpec, read_blueprint
from proving_ground.capability.kube import VncStream
from proving_ground.capability.kubernetes_client import KubernetesApiClient
from proving_ground.capability.models import to_dns_label
from proving_ground.database import get_db
from proving_ground.models.blueprint import RangeInstance
from proving_ground.models.event import EventParticipant
from proving_ground.models.event_log import EventType
from proving_ground.models.range import Range, RangeStatus
from proving_ground.models.user import User
from proving_ground.services import kubernetes_range_service as svc
from proving_ground.services.event_service import EventService
from proving_ground.services.kubernetes_range_service import RANGE_ID_LABEL
from proving_ground.utils.security import (
    WS_TICKET_COOKIE,
    WS_TICKET_MINUTES,
    create_ws_ticket,
    verify_ws_ticket,
    ws_ticket_cookie_kwargs,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["kubernetes"])

KUBEVIRT_GROUP = "kubevirt.io"
KUBEVIRT_VERSION = "v1"
MACHINE_PLURAL = "virtualmachines"
INSTANCE_PLURAL = "virtualmachineinstances"

# What the API server owns on an object it hands back. A read-modify-write that sends them back
# is rejected as a create and, once it is not, pins the object to a version that has moved on.
_SERVER_OWNED_METADATA = frozenset(
    {
        "resourceVersion",
        "uid",
        "creationTimestamp",
        "generation",
        "managedFields",
        "selfLink",
    }
)

# The ws-ticket scope for this socket. `api/websocket.py` names its sockets with one resource id
# and this one is named by two, so the pair goes into the resource half rather than inventing a
# second ticket type -- `verify_ws_ticket` compares the whole scope string, so a ticket for one
# machine cannot verify against another, or against any Era A socket.
WS_TICKET_KIND = "vnc-k8s"

_NOT_ENTITLED = "You are not entitled to this range's consoles"

__all__ = [
    "WS_TICKET_KIND",
    "check_range_console_access",
    "hidden_machines_for",
    "machine_keys_of",
    "machine_visibility_view",
    "pump",
    "range_machine_counts",
    "router",
    "workload_socket_path",
    "workloads_of",
]


def _ws_resource(range_id: UUID, machine: str) -> str:
    """The (range, machine) pair a ticket opens, as one comparable string."""
    return f"{range_id}/{machine}"


def workload_socket_path(range_id: UUID, machine: str) -> str:
    """The socket's path, unmounted. The cookie's scope and the client's URL are both this.

    Both are built from the machine's DNS label rather than from whatever the caller typed,
    because a cookie is matched against the path the browser really dials: a client that
    percent-encoded a display name would send the handshake somewhere the cookie was never
    scoped to, and the socket would refuse a ticket that was perfectly good.
    """
    return f"/ws/vnc/k8s/{range_id}/{machine}"


def check_range_console_access(
    range_obj: Range, user: User, db: Session, detail: str = _NOT_ENTITLED
) -> None:
    """May this user open ANY of this range's consoles? Raises 403 if not.

    Admin, the range's owner, the learner it is assigned to, or a participant in its training
    event. Explicit, because the visibility model's "untagged is public" is the wrong answer to
    "may I take control of this machine".

    `detail` is what the caller wants the refusal to say. The rule is the same for the listing and
    for the console, but "you are not entitled to this range's consoles" is a confusing answer to
    a request for a machine list, and a refusal nobody understands is a support ticket.

    Entitlement is only half the question -- see `hidden_machines_for` for the other half, which
    decides whether a machine this user is entitled to is nonetheless hidden from them.
    """
    entitled = (
        user.is_admin or range_obj.created_by == user.id or range_obj.assigned_to_user_id == user.id
    )
    if not entitled and range_obj.training_event_id:
        entitled = (
            db.query(EventParticipant)
            .filter(
                EventParticipant.event_id == range_obj.training_event_id,
                EventParticipant.user_id == user.id,
            )
            .first()
            is not None
        )
    if not entitled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def hidden_machines_for(range_obj: Range, user: User, db: Session) -> set[str]:
    """Which of this range's machines are hidden from `user`, as stored keys.

    A key is whatever the visibility record holds: a VM row's id on Era A, a workload's DNS
    label on Era B. Both live in the same `hidden_vm_ids` column, because the question the
    instructor is answering -- "which machines may this learner open" -- does not change with
    the substrate, and a second column would be a second thing to forget to enforce.

    The Era B console never asked this at all: an instructor hid a machine, was told it was
    hidden, and the learner went on getting a console for it. A control that reports success and
    changes nothing is worse than no control, so every Era B path that lists or opens a machine
    goes through here.

    The rule is `vms.can_access_vm_console`'s, inverted from "may I see this one" to "which are
    hidden": a role grants no entitlement, but holding admin, engineer or evaluator means nothing
    is hidden from you, and an event participant's list and the range's list both bind -- either
    one can hide a machine.
    """
    if user.is_admin or user.has_any_role("engineer", "evaluator"):
        return set()

    hidden: set[str] = set()
    if range_obj.training_event_id:
        participant = (
            db.query(EventParticipant)
            .filter(
                EventParticipant.event_id == range_obj.training_event_id,
                EventParticipant.user_id == user.id,
            )
            .first()
        )
        if participant:
            hidden |= {str(key) for key in (participant.hidden_vm_ids or [])}
    if range_obj.assigned_to_user_id == user.id:
        hidden |= {str(key) for key in (range_obj.hidden_vm_ids or [])}
    return hidden


def _blueprint_of(db: Session, range_obj: Range):
    instance = db.query(RangeInstance).filter(RangeInstance.range_id == range_obj.id).first()
    config = instance.blueprint.config if instance and instance.blueprint else {}
    return read_blueprint(config or {})


def machine_keys_of(db: Session, range_obj: Range) -> list[str] | None:
    """This range's machine keys on Era B, or None when the VM rows are the answer instead.

    Cluster-free on purpose: the keys come from the blueprint, so an instructor can still hide a
    machine on a range that has not been deployed yet, or whose cluster is not answering.
    """
    if not is_kubernetes():
        return None
    blueprint = _blueprint_of(db, range_obj)
    if not blueprint.deployable_on_kubernetes:
        return None
    return [to_dns_label(w.name) for w in blueprint.workloads]


def machine_visibility_view(db: Session, range_obj: Range) -> list[dict[str, str]] | None:
    """The machines the visibility panel offers on Era B, with live status where there is one.

    Returns None on Era A, and on an Era A blueprint sitting on a Kubernetes host -- neither has
    machines this substrate knows how to hide, and inventing an empty list would tell the
    instructor there is nothing to hide rather than that this is the wrong question.

    A cluster read that fails is not fatal: the machines are still listed, with an unknown
    status. Refusing to draw the panel because status is unavailable would take the control away
    at exactly the moment someone needs it.
    """
    keys = machine_keys_of(db, range_obj)
    if keys is None:
        return None
    statuses: dict[str, str] = {}
    try:
        statuses = asyncio.run(_machine_statuses(db, range_obj))
    except Exception as exc:  # noqa: BLE001 - an unreachable cluster costs status, not the panel
        logger.warning("range %s: machine status unavailable: %s", range_obj.id, exc)
    return [{"id": key, "hostname": key, "status": statuses.get(key, "unknown")} for key in keys]


async def _machine_statuses(db: Session, range_obj: Range) -> dict[str, str]:
    blueprint = _blueprint_of(db, range_obj)
    placement = svc.placement_for_range(range_obj, list(blueprint.capabilities))
    kube = await KubernetesApiClient.connect()
    try:
        states = await svc._lifecycle(kube).workload_states(blueprint.workloads, placement)
    finally:
        await kube.close()
    return {s.name: s.status for s in states}


def range_machine_counts(db: Session, range_ids: Sequence[UUID]) -> dict[UUID, tuple[int, int]]:
    """(networks, machines) for each Era B range in the list, in one query for the whole page.

    The ranges list derived both counts from `Range.networks` and `Range.vms`, which Era B never
    creates -- so every running Kubernetes range advertised "0 networks / 0 VMs" beside a range
    with machines plainly up. The blueprint is what a range is made of on this substrate, and
    reading it here costs one query however many ranges are listed; asking the cluster per range
    would not survive a list of fifty.
    """
    if not range_ids:
        return {}
    instances = (
        db.query(RangeInstance)
        .options(joinedload(RangeInstance.blueprint))
        .filter(RangeInstance.range_id.in_(list(range_ids)))
        .all()
    )
    counts: dict[UUID, tuple[int, int]] = {}
    parsed: dict[UUID, tuple[int, int] | None] = {}
    for instance in instances:
        blueprint = instance.blueprint
        if blueprint is None:
            continue
        if blueprint.id not in parsed:
            parsed[blueprint.id] = _spec_counts(blueprint.config or {})
        if parsed[blueprint.id] is not None:
            counts[instance.range_id] = parsed[blueprint.id]
    return counts


def _spec_counts(config: dict[str, Any]) -> tuple[int, int] | None:
    try:
        spec: RangeSpec = read_blueprint(config)
    except ValueError:
        # A blueprint this engine cannot read is not a count of zero; leave the row alone.
        return None
    if not spec.deployable_on_kubernetes:
        return None
    return (len(spec.networks), len(spec.workloads))


async def workloads_of(db: Session, range_obj: Range, viewer: User | None = None) -> dict[str, Any]:
    """The range's workloads with their live state, or `substrate: dind` for an Era A range.

    `viewer` is who is asking. Machines hidden from them are left out entirely rather than
    listed as unavailable, which is what hiding means and what the Era A listing already did.
    Omitting it lists everything, for callers that have already decided the question.
    """
    blueprint = _blueprint_of(db, range_obj)
    if not is_kubernetes() or not blueprint.deployable_on_kubernetes:
        return {"substrate": "dind", "workloads": []}
    hidden = hidden_machines_for(range_obj, viewer, db) if viewer is not None else set()
    placement = svc.placement_for_range(range_obj, list(blueprint.capabilities))
    kube = await KubernetesApiClient.connect()
    try:
        states = await svc._lifecycle(kube).workload_states(blueprint.workloads, placement)
    finally:
        await kube.close()
    from proving_ground.api.kubernetes_apps import apps_of

    by_name = {to_dns_label(w.name): w for w in blueprint.workloads}
    return {
        "substrate": "kubernetes",
        "namespace": placement.namespace,
        "apps": apps_of(db, range_obj),
        "workloads": [
            {
                "name": s.name,
                "os_family": by_name[s.name].os_family.value,
                "os_version": by_name[s.name].os_version,
                "cpus": by_name[s.name].cpus,
                "memory_mb": by_name[s.name].memory_mb,
                "status": s.status,
                "addresses": dict(s.addresses),
                "console_available": s.status == "Running",
            }
            for s in states
            if s.name not in hidden
        ],
    }


@router.get("/range-machines/summary")
async def machine_summary(db: DBSession, current_user: CurrentUser):
    """How many machines are running across every range this install can see.

    The dashboard asked each range for its `vms` and summed them, which on Kubernetes is always
    zero -- a range there has no VM rows, so the tile read "Running VMs: 0" beside a range whose
    VM was plainly Running. One labelled, cluster-wide list answers it instead, in a single
    request however many ranges exist.

    Not under /ranges/: that prefix has a `{range_id}` route whose session dependency runs before
    the path is validated, so a literal segment there is captured and rejected.

    Counted over the ranges the caller may see, not over the cluster. The Era A tile summed the
    VM rows of the ranges in the caller's own list, so it never told a learner anything about
    somebody else's exercise; one cluster-wide count would have, and a machine total is an
    inventory fact about the install. The cluster call stays cluster-wide -- one request however
    many ranges exist -- and the label each instance carries is what narrows it afterwards.
    """
    if not is_kubernetes():
        return {"substrate": "dind", "running": None, "total": None}
    kube = await KubernetesApiClient.connect()
    try:
        instances = await kube.list_cluster_custom_objects(
            group=KUBEVIRT_GROUP,
            version=KUBEVIRT_VERSION,
            plural=INSTANCE_PLURAL,
            label_selector=RANGE_ID_LABEL,
        )
    finally:
        await kube.close()
    if not current_user.is_admin:
        # An administrator's count is the cluster's, deliberately: an instance whose range row is
        # gone is exactly what an operator needs to see, and filtering it out would hide the leak
        # it represents. Everyone else is counted against their own list.
        visible = {
            str(row.id)
            for row in filter_by_visibility(
                db.query(Range.id), "range", current_user, db, Range
            ).all()
        }
        instances = [
            i
            for i in instances
            if ((i.get("metadata") or {}).get("labels") or {}).get(RANGE_ID_LABEL) in visible
        ]
    running = sum(1 for i in instances if ((i.get("status") or {}).get("phase")) == "Running")
    return {"substrate": "kubernetes", "running": running, "total": len(instances)}


@router.get("/ranges/{range_id}/workloads")
async def list_workloads(range_id: UUID, db: DBSession, current_user: CurrentUser):
    """What this range is made of, for whoever may actually work in it.

    Authorised as a console, not as a listing. `check_resource_access` is a visibility model --
    it answers "may I see this range in my list", and on a PUBLIC range it says yes to every
    account on the install. What comes back here is not a name and a status: it is the range's
    namespace, every machine's internal addresses, and the applications published for it. That is
    the map of somebody else's exercise, and the rule for it is the one that governs opening a
    console into that exercise.
    """
    range_obj = db.query(Range).filter(Range.id == range_id).first()
    if not range_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Range not found")
    check_range_console_access(
        range_obj,
        current_user,
        db,
        detail=(
            "You are not working in this range. Ask its owner to assign it to you, or to add "
            "you to its training event."
        ),
    )
    return await workloads_of(db, range_obj, viewer=current_user)


class MachinePowerRequest(BaseModel):
    """What to do to one machine."""

    action: Literal["start", "stop", "restart"]


@router.post("/ranges/{range_id}/workloads/{workload}/power")
async def set_machine_power(
    range_id: UUID,
    workload: str,
    request: MachinePowerRequest,
    db: DBSession,
    current_user: CurrentUser,
):
    """Start, stop or restart a single machine.

    Only the range as a whole could be stopped and started, so one machine that had wedged cost
    the exercise a full restart -- every other learner's work included. Authorised as a change to
    the range, because that is what it is: a learner assigned to a lab may open its consoles, not
    power its machines off.
    """
    range_obj = db.query(Range).filter(Range.id == range_id).first()
    if not range_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Range not found")
    check_resource_control("range", range_id, current_user, db, range_obj.created_by)

    keys = machine_keys_of(db, range_obj)
    if keys is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Per-machine power is a Kubernetes operation and this range is not one",
        )
    name = _dns_label_or_none(workload)
    if name is None or name not in keys:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such machine in this range"
        )

    blueprint = _blueprint_of(db, range_obj)
    placement = svc.placement_for_range(range_obj, list(blueprint.capabilities))
    kube = await KubernetesApiClient.connect()
    try:
        changed = await _apply_power(kube, placement.namespace, name, request.action)
        range_status = await _recompute_range_status(
            kube, placement.namespace, keys, name, request.action, range_obj, db
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - the cluster's refusal is the answer, not a 500
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"kubernetes {request.action} of {name} failed: {exc}",
        ) from exc
    finally:
        await kube.close()
    # Era A wrote one of these for every per-VM power change (v0.45.0 api/vms.py:2206, :2299,
    # :3163). Era B wrote none at all, so "who turned this machine off" was unanswerable -- the
    # range's own stop still logged a stop for every machine in the spec, including ones that
    # were never running.
    EventService(db).log_event(
        range_id=range_obj.id,
        event_type=_POWER_EVENT[request.action],
        message=f"Machine {name} {_POWER_VERB[request.action]} by {current_user.username}",
        user_id=current_user.id,
    )
    logger.info(
        "machine %s/%s %s by %s", placement.namespace, name, request.action, current_user.username
    )
    return {
        "namespace": placement.namespace,
        "machine": name,
        "action": request.action,
        "changed": changed,
        "range_status": range_status.value,
    }


_POWER_EVENT = {
    "start": EventType.VM_STARTED,
    "stop": EventType.VM_STOPPED,
    "restart": EventType.VM_RESTARTED,
}
_POWER_VERB = {"start": "started", "stop": "stopped", "restart": "restarted"}


async def _recompute_range_status(
    kube: KubernetesApiClient,
    namespace: str,
    names: Sequence[str],
    changed_name: str,
    action: str,
    range_obj: Range,
    db: Session,
) -> RangeStatus:
    """Bring the range's status back in line with its machines, as Era A did.

    Without this a range whose every machine is stopped still reported `running`, so the learner
    portal admitted a student to a lab with nothing in it; and starting one machine on a stopped
    range left the range `stopped`, which hides the Workloads panel the operator had just used
    (RangeDetail.tsx gates it on range status). Era A's two rules, unchanged:
    any machine running and the range is stopped or draft -> running; no machine running and the
    range is running -> stopped.

    It counts the DECLARED state -- `spec.running`, the field the power write just set -- not
    `status.printableStatus`. KubeVirt does not move printableStatus while the power request is
    still open, so a live-state count reads the value from before the change and moves the range
    one action late. The machine just acted on is not re-read at all: its intended state is known
    here and a read could still be serving the old value.
    """
    running: dict[str, bool] = {changed_name: action != "stop"}
    for other in names:
        if other == changed_name:
            continue
        machine = await kube.get_custom_object(
            group=KUBEVIRT_GROUP,
            version=KUBEVIRT_VERSION,
            plural=MACHINE_PLURAL,
            namespace=namespace,
            name=other,
        )
        # A machine the blueprint declares but the cluster has not got is not running.
        running[other] = (
            bool((machine.get("spec") or {}).get("running", True)) if machine else False
        )

    any_running = any(running.values())
    if any_running and range_obj.status in (RangeStatus.STOPPED, RangeStatus.DRAFT):
        range_obj.status = RangeStatus.RUNNING
        db.commit()
        logger.info("range %s is running again: %s was started", range_obj.id, changed_name)
    elif not any_running and range_obj.status == RangeStatus.RUNNING:
        range_obj.status = RangeStatus.STOPPED
        db.commit()
        logger.info("range %s is stopped: every machine is stopped", range_obj.id)
    return range_obj.status


async def _apply_power(kube: KubernetesApiClient, namespace: str, name: str, action: str) -> bool:
    """Do it, and say whether anything actually moved.

    `changed: false` is the answer to "stop a machine that was already stopped", and it has to be
    distinguishable from success or the UI reports a no-op as a power cycle.
    """
    machine = await kube.get_custom_object(
        group=KUBEVIRT_GROUP,
        version=KUBEVIRT_VERSION,
        plural=MACHINE_PLURAL,
        namespace=namespace,
        name=name,
    )
    if machine is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{name} is not deployed; deploy the range first",
        )
    running = bool((machine.get("spec") or {}).get("running", True))

    if action == "restart":
        if not running:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{name} is stopped; start it rather than restarting it",
            )
        # KubeVirt has no restart field. Deleting the instance is what `virtctl restart` does:
        # the VirtualMachine still declares itself running, so virt-controller builds a new one
        # from the same disks.
        await kube.delete_custom_object(
            group=KUBEVIRT_GROUP,
            version=KUBEVIRT_VERSION,
            plural=INSTANCE_PLURAL,
            namespace=namespace,
            name=name,
        )
        return True

    wanted = action == "start"
    if running == wanted:
        return False
    await kube.apply_custom_object(
        group=KUBEVIRT_GROUP,
        version=KUBEVIRT_VERSION,
        plural=MACHINE_PLURAL,
        namespace=namespace,
        name=name,
        body=_declared(machine, running=wanted),
    )
    return True


def _declared(machine: dict[str, Any], *, running: bool) -> dict[str, Any]:
    """The machine as it was read back, minus what the API server owns, plus the new power state."""
    body = {k: v for k, v in machine.items() if k != "status"}
    body["metadata"] = {
        k: v for k, v in (machine.get("metadata") or {}).items() if k not in _SERVER_OWNED_METADATA
    }
    body["spec"] = {**(machine.get("spec") or {}), "running": running}
    return body


def _dns_label_or_none(workload: str) -> str | None:
    try:
        return to_dns_label(workload)
    except ValueError:
        # Nothing addressable came out of it, so it names no machine here.
        return None


async def pump(browser: WebSocket, vm: VncStream) -> None:
    """Bytes both ways until either side closes. The first side to close ends both."""

    async def to_vm() -> None:
        while True:
            data = await browser.receive_bytes()
            await vm.send(data)

    async def to_browser() -> None:
        while True:
            frame = await vm.receive()
            if frame is None:
                return
            await browser.send_bytes(frame)

    tasks = [asyncio.ensure_future(to_vm()), asyncio.ensure_future(to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.post("/ranges/{range_id}/workloads/{workload}/console-ticket")
def issue_workload_console_ticket(
    range_id: UUID,
    workload: str,
    request: Request,
    response: Response,
    db: DBSession,
    current_user: CurrentUser,
):
    """Exchange the session for a ticket that opens ONE machine's console socket.

    This is where entitlement is decided; the socket checks the ticket and then asks the same
    questions again, because five minutes is short but it is not zero.

    Nothing secret is in the body. What comes back is a cookie scoped to the one socket path,
    which the handshake sends by itself, and the path to dial -- so the client never builds the
    URL out of a name that might not match the scope the cookie was written for.
    """
    range_obj = db.query(Range).filter(Range.id == range_id).first()
    if not range_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Range not found")
    check_range_console_access(range_obj, current_user, db)
    name = _console_target(db, range_obj, workload, current_user)

    # This router is mounted under /api/v1 in the app and bare in tests that exercise it alone.
    # Taking the prefix off this request's own path keeps the cookie's scope right in both
    # without either being written down here; a cookie scoped to the wrong prefix is never sent,
    # and the socket refuses a ticket the browser is holding.
    path = request.url.path
    marker = path.find("/ranges/")
    socket_path = f"{path[:marker] if marker >= 0 else ''}{workload_socket_path(range_id, name)}"

    response.set_cookie(
        value=create_ws_ticket(current_user.id, WS_TICKET_KIND, _ws_resource(range_id, name)),
        **ws_ticket_cookie_kwargs(socket_path),
    )
    response.headers["Cache-Control"] = "no-store"
    return {"path": socket_path, "expires_in": WS_TICKET_MINUTES * 60}


def _console_target(db: Session, range_obj: Range, workload: str, user: User) -> str:
    """The machine key `workload` names in this range, or a 403/404 saying why it does not.

    Hidden is checked after existence, so a learner who was never meant to see an instructor's
    box is told it is not theirs rather than that it does not exist -- the same order the socket
    uses, and the same order the range listing implies.
    """
    blueprint = _blueprint_of(db, range_obj)
    name = _dns_label_or_none(workload)
    if (
        not blueprint.deployable_on_kubernetes
        or name is None
        or name not in {to_dns_label(w.name) for w in blueprint.workloads}
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such machine in this range"
        )
    if name in hidden_machines_for(range_obj, user, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This machine is not available to you"
        )
    return name


@router.websocket("/ws/vnc/k8s/{range_id}/{workload}")
async def workload_vnc(websocket: WebSocket, range_id: UUID, workload: str):
    """The machine's framebuffer, for a browser holding a ticket for this machine.

    No credential in the URL, and a session token is refused even though it would identify the
    right user: `verify_ws_ticket` takes only `typ == "ws"`, and the ticket arrives in a cookie
    the browser scoped to this path. A URL is logged by every proxy it crosses and kept in the
    browser's history, so a token that opens the whole API had no business being in one.

    Everything the ticket already proved is asked again before a byte moves, because it was
    minted up to five minutes ago and an instructor can hide a machine in less than that.
    """
    await websocket.accept(subprotocol="binary")
    db = next(get_db())
    try:
        name = _dns_label_or_none(workload)
        user_id = (
            verify_ws_ticket(
                websocket.cookies.get(WS_TICKET_COOKIE),
                WS_TICKET_KIND,
                _ws_resource(range_id, name),
            )
            if name
            else None
        )
        if user_id is None:
            # One answer for "no ticket", "expired", "another machine's ticket" and "that is an
            # access token": which it was is not the browser's business, and the client's move
            # is the same in every case. The reason names that move, because a console that just
            # closes looks like a broken cluster.
            await websocket.close(
                code=4001,
                reason="No valid console ticket -- POST this machine's console-ticket first",
            )
            return
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.is_active:
            # `get_current_user` refuses a deactivated account on every HTTP route; a console is
            # the last thing one should keep after that.
            await websocket.close(code=4001, reason="No valid console ticket")
            return
        range_obj = db.query(Range).filter(Range.id == range_id).first()
        if not range_obj:
            await websocket.close(code=4004, reason="Range not found")
            return
        try:
            check_range_console_access(range_obj, user, db)
        except HTTPException as exc:
            await websocket.close(code=4003, reason=str(exc.detail))
            return
        blueprint = _blueprint_of(db, range_obj)
        if not blueprint.deployable_on_kubernetes or name not in {
            to_dns_label(w.name) for w in blueprint.workloads
        }:
            await websocket.close(code=4004, reason="No such workload in this range")
            return
        if name in hidden_machines_for(range_obj, user, db):
            # Entitled to the range, but this machine was hidden from them for this exercise --
            # an instructor's or red team's box. Refused after the existence check so the reason
            # a learner sees is "not yours", not "no such machine".
            await websocket.close(code=4003, reason="This machine is not available to you")
            return
        placement = svc.placement_for_range(range_obj, list(blueprint.capabilities))
    finally:
        db.close()

    kube = await KubernetesApiClient.connect()
    try:
        async with kube.open_vnc(namespace=placement.namespace, name=name) as vm:
            logger.info("console opened: %s/%s by %s", placement.namespace, name, user.username)
            await pump(websocket, vm)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - the reason goes to the browser, not a 500 page
        logger.warning("console %s/%s ended: %s", placement.namespace, name, exc)
        await _close(websocket, 4000, str(exc)[:120])
    finally:
        await kube.close()
        await _close(websocket, 1000, "")


async def _close(websocket: WebSocket, code: int, reason: str) -> None:
    """Close if still open. Starlette raises on a second close, and the browser hanging up
    first is the normal way a console ends."""
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    try:
        await websocket.close(code=code, reason=reason)
    except RuntimeError:
        pass
