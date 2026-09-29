# proving_ground/tasks/deployment.py
"""Async deployment tasks using Dramatiq."""

import asyncio
import base64
import contextlib
import dramatiq
import json
import logging
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import UUID

import os

from sqlalchemy.orm import Session

from proving_ground.database import get_session_local
from proving_ground.models.range import Range, RangeStatus
from proving_ground.models.network import Network
from proving_ground.models.vm import VM, VMStatus
from proving_ground.models.base_image import BaseImage
from proving_ground.models.golden_image import GoldenImage
from proving_ground.models.snapshot import Snapshot
from proving_ground.models.router import RangeRouter, RouterStatus
from proving_ground.models.event_log import EventType
from proving_ground.config import get_settings
from proving_ground.services.event_service import EventService

logger = logging.getLogger(__name__)

settings = get_settings()


# --- Era B lifecycle progress ---------------------------------------------------------------
#
# A lifecycle operation on the cluster is one call into `kubernetes_range_service`, so between
# DEPLOYMENT_STARTED and DEPLOYMENT_COMPLETED the deployment panel had nothing to read. It showed
# a Docker-in-Docker container pending at 0% for the whole of a successful Kubernetes deploy,
# because its resource list came from the Era A plan. These are the stages the cluster is actually
# asked for, in the order the service asks for them.

DEPLOY_STAGES = (
    "Creating Namespace",
    "Creating Network Attachments",
    "Creating Machines",
    "Installing Capabilities",
    "Waiting for Machines",
)
START_STAGES = ("Starting Machines", "Waiting for Machines")
STOP_STAGES = ("Stopping Machines",)

# How often the watcher asks the cluster what has appeared. Slow enough not to add load to an
# API server that is already building a range, fast enough that a stage is not missed entirely.
_PROGRESS_POLL_SECONDS = 3.0

# Multus' own API, named here rather than imported out of `capability.lifecycle`, which keeps
# these private -- the same choice `api/kubernetes_console.py` makes for KubeVirt's.
_NAD_GROUP, _NAD_VERSION, _NAD_PLURAL = "k8s.cni.cncf.io", "v1", "network-attachment-definitions"
_KUBEVIRT_GROUP, _KUBEVIRT_VERSION, _MACHINE_PLURAL = "kubevirt.io", "v1", "virtualmachines"


def _era_b_extra(**fields: Any) -> str:
    return json.dumps({"substrate": "kubernetes", **fields})


class _KubernetesProgress:
    """Writes what the cluster is doing as the events the deployment panel reads.

    Resources are keyed by name in `extra_data` rather than by `vm_id` or `network_id`: Era B
    creates no VM or Network rows, so there is no row for an event to point at, and the blueprint
    name is what both the panel and the activity log can show a person.

    Every write is best-effort. A progress report that fails must cost the report and not the
    operation it is reporting on.
    """

    def __init__(self, db: Session, range_id: UUID, operation: str, stages: Sequence[str]) -> None:
        self._db = db
        self._range_id = range_id
        self._operation = operation
        self._stages = tuple(stages)

    def step(self, stage: int, detail: str) -> None:
        """Record that the operation has reached stage `stage` (1-based)."""
        name = self._stages[stage - 1] if 1 <= stage <= len(self._stages) else detail
        self._write(
            EventType.DEPLOYMENT_STEP,
            f"[Stage {stage}/{len(self._stages)}] {detail}",
            operation=self._operation,
            stage=stage,
            total_stages=len(self._stages),
            stage_name=name,
        )

    def resource(self, event_type: EventType, name: str, message: str) -> None:
        self._write(event_type, message, operation=self._operation, resource=name)

    def _write(self, event_type: EventType, message: str, **fields: Any) -> None:
        try:
            EventService(self._db).log_event(
                range_id=self._range_id,
                event_type=event_type,
                message=message,
                extra_data=_era_b_extra(**fields),
            )
        except Exception as exc:  # noqa: BLE001 - see the class docstring
            logger.warning("range %s: could not record progress: %s", self._range_id, exc)


def _era_b_spec(db: Session, range_obj: Range):
    """The blueprint this Era B range is made of, or None if it has none this engine can read."""
    from proving_ground.capability.blueprint import read_blueprint
    from proving_ground.models.blueprint import RangeInstance

    instance = db.query(RangeInstance).filter(RangeInstance.range_id == range_obj.id).first()
    config = instance.blueprint.config if instance and instance.blueprint else {}
    try:
        spec = read_blueprint(config or {})
    except ValueError:
        return None
    return spec if spec.deployable_on_kubernetes else None


async def _machine_status(kube, namespace: str, key: str) -> str | None:
    """KubeVirt's `printableStatus` for one machine, or None while the object does not exist."""
    machine = await kube.get_custom_object(
        group=_KUBEVIRT_GROUP,
        version=_KUBEVIRT_VERSION,
        plural=_MACHINE_PLURAL,
        namespace=namespace,
        name=key,
    )
    if machine is None:
        return None
    return ((machine.get("status") or {}).get("printableStatus")) or "Unknown"


async def _watch_machines(
    kube,
    report: _KubernetesProgress,
    spec,
    namespace: str,
    *,
    created: set[str],
    running: set[str],
) -> None:
    """One pass over the machines, reporting each one the first time it appears and runs."""
    from proving_ground.capability.models import to_dns_label

    for workload in spec.workloads:
        key = to_dns_label(workload.name)
        status = await _machine_status(kube, namespace, key)
        if status is None:
            continue
        if workload.name not in created:
            created.add(workload.name)
            await asyncio.to_thread(
                report.resource,
                EventType.VM_CREATING,
                workload.name,
                f"Machine '{workload.name}' created ({status})",
            )
        if status == "Running" and workload.name not in running:
            running.add(workload.name)
            await asyncio.to_thread(
                report.resource,
                EventType.VM_STARTED,
                workload.name,
                f"Machine '{workload.name}' is Running",
            )


async def _watch_deploy(report: _KubernetesProgress, spec, placement) -> None:
    """Watch the range's namespace fill up, reporting each stage as it is reached.

    The deploy itself is a single service call, so this runs beside it and reads the cluster
    rather than being told. It never finishes on its own: the caller cancels it when the deploy
    returns, which is also what stops it when a deploy fails halfway.
    """
    from proving_ground.capability.flux import HELM_GROUP, HELM_PLURAL, HELM_VERSION
    from proving_ground.capability.kubernetes_client import KubernetesApiClient

    namespace = placement.namespace
    kube = await KubernetesApiClient.connect()
    try:
        await asyncio.to_thread(report.step, 1, f"Creating namespace {namespace}")
        while not await kube.namespace_exists(namespace):
            await asyncio.sleep(_PROGRESS_POLL_SECONDS)

        await asyncio.to_thread(report.step, 2, "Creating network attachments")
        for network in spec.networks:
            await asyncio.to_thread(
                report.resource,
                EventType.NETWORK_CREATING,
                network.name,
                f"Creating attachment for network '{network.name}'",
            )
        pending = list(spec.networks)
        while pending:
            still_pending = []
            for network in pending:
                attachment = await kube.get_custom_object(
                    group=_NAD_GROUP,
                    version=_NAD_VERSION,
                    plural=_NAD_PLURAL,
                    namespace=namespace,
                    name=network.attachment_name,
                )
                if attachment is None:
                    still_pending.append(network)
                else:
                    await asyncio.to_thread(
                        report.resource,
                        EventType.NETWORK_CREATED,
                        network.name,
                        f"Attachment {network.attachment_name} created",
                    )
            pending = still_pending
            if pending:
                await asyncio.sleep(_PROGRESS_POLL_SECONDS)

        await asyncio.to_thread(report.step, 3, "Creating machines")
        created: set[str] = set()
        running: set[str] = set()
        while len(created) < len(spec.workloads):
            await _watch_machines(kube, report, spec, namespace, created=created, running=running)
            if len(created) < len(spec.workloads):
                await asyncio.sleep(_PROGRESS_POLL_SECONDS)

        await asyncio.to_thread(report.step, 4, "Installing capabilities")
        for capability in spec.capabilities:
            while True:
                release = await kube.get_custom_object(
                    group=HELM_GROUP,
                    version=HELM_VERSION,
                    plural=HELM_PLURAL,
                    namespace=namespace,
                    name=capability.name,
                )
                conditions = ((release or {}).get("status") or {}).get("conditions") or []
                if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
                    break
                await _watch_machines(
                    kube, report, spec, namespace, created=created, running=running
                )
                await asyncio.sleep(_PROGRESS_POLL_SECONDS)

        await asyncio.to_thread(report.step, 5, "Waiting for machines to reach Running")
        while len(running) < len(spec.workloads):
            await _watch_machines(kube, report, spec, namespace, created=created, running=running)
            if len(running) < len(spec.workloads):
                await asyncio.sleep(_PROGRESS_POLL_SECONDS)
    finally:
        await kube.close()


async def _watch_start(report: _KubernetesProgress, spec, placement) -> None:
    """The start equivalent: the machines are already there, so only Running is in question."""
    from proving_ground.capability.kubernetes_client import KubernetesApiClient

    kube = await KubernetesApiClient.connect()
    try:
        await asyncio.to_thread(report.step, 2, "Waiting for machines to reach Running")
        created: set[str] = set()
        running: set[str] = set()
        while len(running) < len(spec.workloads):
            await _watch_machines(
                kube, report, spec, placement.namespace, created=created, running=running
            )
            if len(running) < len(spec.workloads):
                await asyncio.sleep(_PROGRESS_POLL_SECONDS)
    finally:
        await kube.close()


async def _with_progress(work, watch) -> Any:
    """Run `work`, with `watch` reporting alongside it until it returns.

    The watcher is cancelled rather than awaited: it has no completion condition of its own, and
    a deploy that failed must not be held open by a watcher still waiting for a machine that is
    never coming.
    """

    async def guarded():
        try:
            await watch
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - progress must never fail the operation
            logger.warning("kubernetes progress watcher stopped: %s", exc)

    watcher = asyncio.ensure_future(guarded())
    # Let the watcher reach its first await before the work starts, so even an operation that
    # finishes quickly has reported the stage it was in.
    await asyncio.sleep(0)
    try:
        return await work
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


@contextlib.contextmanager
def _progress_session():
    """A database session of the progress reporter's own.

    The reporter writes from a worker thread while the operation it is reporting on is using its
    own session on the loop's thread. A SQLAlchemy Session is not thread-safe, and sharing one
    here would corrupt the deploy's transaction rather than merely lose an event.
    """
    db = get_session_local()()
    try:
        yield db
    finally:
        db.close()


def _run_era_b(coro) -> Any:
    """Run one Era B coroutine on a loop of its own, as the worker has no loop running."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        # The progress reporter writes through `asyncio.to_thread`, and closing the loop does not
        # wait for its executor. Without this a last event could still be being written on a
        # thread while the session it is writing on is closed underneath it.
        with contextlib.suppress(Exception):
            loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


def _era_b_failed(db: Session, range_id: UUID, verb: str, exc: Exception) -> None:
    """Put the reason where a person will find it: the range's status and its activity record.

    A failure that only reaches the worker's log is a range stuck in a transitional status with
    nothing to explain it, which is what start and stop did before they moved off the request.

    A message already on the row is left alone. The code that raised knows more about what went
    wrong than this handler does -- where it has said so, `str(exc)` is the poorer answer.
    """
    # The operation may have left the session mid-transaction, and a query on it would raise
    # here and take the only record of the failure with it.
    with contextlib.suppress(Exception):
        db.rollback()
    range_obj = db.query(Range).filter(Range.id == range_id).first()
    if not range_obj:
        return
    range_obj.status = RangeStatus.ERROR
    if not range_obj.error_message:
        range_obj.error_message = str(exc)[:1000]
    db.commit()
    try:
        EventService(db).log_event(
            range_id=range_id,
            event_type=EventType.DEPLOYMENT_FAILED,
            message=f"Kubernetes {verb} failed: {str(exc)[:500]}",
            extra_data=_era_b_extra(operation=verb),
        )
    except Exception:  # noqa: BLE001 - the status already carries the reason
        logger.exception("range %s: could not record the %s failure", range_id, verb)


def _deploy_range_on_kubernetes(range_id: str) -> None:
    """Era B: placement by policy, capabilities through CapabilityRuntime.

    A separate function rather than a branch threaded through the DinD body below. Era A is frozen
    at v0.45.0 and the whole point of MIG-3 is to delete it -- interleaving the two paths would
    make that deletion a rewrite instead of a removal.
    """
    from proving_ground.services.kubernetes_range_service import (
        deploy_range_on_kubernetes,
        placement_for_range,
    )

    logger.info(f"Starting Kubernetes deployment for range {range_id}")
    db = get_session_local()()
    try:
        range_obj = db.query(Range).filter(Range.id == UUID(range_id)).first()
        if not range_obj:
            logger.error(f"Range {range_id} not found")
            return

        range_obj.status = RangeStatus.DEPLOYING
        # Cleared here and not only in the endpoint: a redeploy dispatched from a training event
        # or an instance reset never passes through it, and a stale reason left on the row would
        # be taken for this attempt's.
        range_obj.error_message = None
        db.commit()

        event_service = EventService(db)
        event_service.log_event(
            range_id=UUID(range_id),
            event_type=EventType.DEPLOYMENT_STARTED,
            message=f"Starting Kubernetes deployment of range '{range_obj.name}'",
            extra_data=_era_b_extra(operation="deploy"),
        )

        spec = _era_b_spec(db, range_obj)
        work = deploy_range_on_kubernetes(db, UUID(range_id))
        if spec is None:
            result = _run_era_b(work)
        else:
            placement = placement_for_range(range_obj, list(spec.capabilities))
            with _progress_session() as progress_db:
                report = _KubernetesProgress(progress_db, UUID(range_id), "deploy", DEPLOY_STAGES)
                result = _run_era_b(_with_progress(work, _watch_deploy(report, spec, placement)))

        # Re-read rather than reuse: the deploy has been using this session for minutes and may
        # have rolled it back to record a failure of its own, which expires what is held here.
        range_obj = db.query(Range).filter(Range.id == UUID(range_id)).first()
        if not range_obj:
            logger.error(f"Range {range_id} deployed but its row is gone")
            return
        now = datetime.now(timezone.utc)
        range_obj.status = RangeStatus.RUNNING
        # A range that deployed to Running has both been deployed and started; leaving these null
        # is why every Kubernetes range showed blank lifecycle timestamps.
        range_obj.deployed_at = now
        range_obj.started_at = now
        range_obj.error_message = None
        db.commit()
        # The placement reason is logged, not just the outcome: 'by policy, not by identity' is
        # only demonstrable if the reason survives into the record.
        event_service.log_event(
            range_id=UUID(range_id),
            event_type=EventType.DEPLOYMENT_COMPLETED,
            message=(
                f"Range '{range_obj.name}' placed on {result['isolation']} "
                f"{result['namespace']} -- {result['reason']}"
            ),
            extra_data=json.dumps({"substrate": "kubernetes", **result}),
        )
        logger.info(f"Range {range_id} deployed on Kubernetes: {result}")
    except Exception as e:
        logger.error(f"Failed to deploy range {range_id} on Kubernetes: {e}")
        _era_b_failed(db, UUID(range_id), "deploy", e)
    finally:
        db.close()


def _start_range_on_kubernetes(range_id: str, user_id: str | None = None) -> None:
    """Era B start, off the request path.

    The endpoint used to run this inline and answer only once every machine reported Running --
    up to ten minutes on an anyio threadpool worker, with no timeout on the browser's end and a
    proxy in front cutting it long before the cluster was done. The endpoint now records the
    intent and returns; this drives it and the range's status is what the UI follows.
    """
    from proving_ground.services.kubernetes_range_service import (
        placement_for_range,
        start_range_on_kubernetes,
    )

    db = get_session_local()()
    try:
        range_obj = db.query(Range).filter(Range.id == UUID(range_id)).first()
        if not range_obj:
            logger.error(f"Range {range_id} not found")
            return

        spec = _era_b_spec(db, range_obj)
        work = start_range_on_kubernetes(db, UUID(range_id))
        if spec is None:
            result = _run_era_b(work)
        else:
            placement = placement_for_range(range_obj, list(spec.capabilities))
            with _progress_session() as progress_db:
                report = _KubernetesProgress(progress_db, UUID(range_id), "start", START_STAGES)
                result = _run_era_b(_with_progress(work, _watch_start(report, spec, placement)))

        range_obj = db.query(Range).filter(Range.id == UUID(range_id)).first()
        if not range_obj:
            logger.error(f"Range {range_id} started but its row is gone")
            return
        range_obj.status = RangeStatus.RUNNING
        range_obj.started_at = datetime.now(timezone.utc)
        range_obj.error_message = None
        db.commit()
        EventService(db).log_event(
            range_id=UUID(range_id),
            event_type=EventType.RANGE_STARTED,
            message=f"Range '{range_obj.name}' started",
            user_id=UUID(user_id) if user_id else None,
            extra_data=_era_b_extra(operation="start", **result),
        )
        logger.info(f"Range {range_id} started on Kubernetes: {result}")
    except Exception as e:
        logger.error(f"Failed to start range {range_id} on Kubernetes: {e}")
        _era_b_failed(db, UUID(range_id), "start", e)
    finally:
        db.close()


def _stop_range_on_kubernetes(range_id: str, user_id: str | None = None) -> None:
    """Era B stop, off the request path, and recorded.

    Stopping is a handful of API calls rather than a wait, but it crosses the cluster API, and a
    range that was stopped left no trace at all before this: no `stopped_at`, no activity row.
    """
    from proving_ground.services.kubernetes_range_service import stop_range_on_kubernetes

    db = get_session_local()()
    try:
        range_obj = db.query(Range).filter(Range.id == UUID(range_id)).first()
        if not range_obj:
            logger.error(f"Range {range_id} not found")
            return

        result = _run_era_b(stop_range_on_kubernetes(db, UUID(range_id)))

        spec = _era_b_spec(db, range_obj)
        report = _KubernetesProgress(db, UUID(range_id), "stop", STOP_STAGES)
        for workload in spec.workloads if spec else ():
            report.resource(
                EventType.VM_STOPPED, workload.name, f"Machine '{workload.name}' stopped"
            )

        range_obj.status = RangeStatus.STOPPED
        range_obj.stopped_at = datetime.now(timezone.utc)
        range_obj.error_message = None
        db.commit()
        EventService(db).log_event(
            range_id=UUID(range_id),
            event_type=EventType.RANGE_STOPPED,
            message=f"Range '{range_obj.name}' stopped",
            user_id=UUID(user_id) if user_id else None,
            extra_data=_era_b_extra(operation="stop", **result),
        )
        logger.info(f"Range {range_id} stopped on Kubernetes: {result}")
    except Exception as e:
        logger.error(f"Failed to stop range {range_id} on Kubernetes: {e}")
        _era_b_failed(db, UUID(range_id), "stop", e)
    finally:
        db.close()


def _teardown_range_on_kubernetes(range_id: str) -> None:
    """Era B teardown: destroy the placement and prove nothing was left behind.

    Same shape as `_deploy_range_on_kubernetes`, for the same reason -- a separate function the
    dispatch below selects, never a branch inside the DinD body.
    """
    from proving_ground.services.kubernetes_range_service import destroy_range_on_kubernetes

    logger.info(f"Starting Kubernetes teardown for range {range_id}")
    db = get_session_local()()
    try:
        range_obj = db.query(Range).filter(Range.id == UUID(range_id)).first()
        if not range_obj:
            logger.error(f"Range {range_id} not found")
            return

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(destroy_range_on_kubernetes(db, UUID(range_id)))
        finally:
            loop.close()

        range_obj.status = RangeStatus.DRAFT
        range_obj.error_message = None
        db.commit()
        EventService(db).log_event(
            range_id=UUID(range_id),
            event_type=EventType.RANGE_TEARDOWN,
            message=(
                f"Range '{range_obj.name}' torn down from {result['isolation']} "
                f"{result['namespace']} -- residue: {result['residue']}"
            ),
            extra_data=json.dumps({"substrate": "kubernetes", **result}),
        )
        logger.info(f"Range {range_id} torn down on Kubernetes: {result}")
    except Exception as e:
        # Residue is a failure here, deliberately: a teardown that stranded cluster RBAC or a
        # volume must not report DRAFT and let the next deploy build on top of it.
        logger.error(f"Failed to tear down range {range_id} on Kubernetes: {e}")
        range_obj = db.query(Range).filter(Range.id == UUID(range_id)).first()
        if range_obj:
            range_obj.status = RangeStatus.ERROR
            range_obj.error_message = str(e)[:1000]
            db.commit()
            try:
                EventService(db).log_event(
                    range_id=UUID(range_id),
                    event_type=EventType.RANGE_TEARDOWN,
                    message=f"Kubernetes teardown failed: {str(e)[:500]}",
                )
            except Exception:
                pass
    finally:
        db.close()


@dramatiq.actor(max_retries=3, min_backoff=1000, queue_name=settings.deploy_queue_name)
def deploy_range_task(range_id: str):
    """
    Async task to deploy a range.

    Dispatches on `settings.range_substrate`: "kubernetes" takes the Era B path, anything else
    takes the DinD path below, whose behaviour is unchanged. A host that sets nothing keeps
    exactly what it had.
    """
    if settings.range_substrate == "kubernetes":
        _deploy_range_on_kubernetes(range_id)
        return

    logger.info(f"Starting DinD deployment for range {range_id}")

    db = get_session_local()()
    try:
        from proving_ground.services.range_deployment_service import get_range_deployment_service

        deployment_service = get_range_deployment_service()

        range_obj = db.query(Range).filter(Range.id == UUID(range_id)).first()
        if not range_obj:
            logger.error(f"Range {range_id} not found")
            return

        # Count resources for event
        networks = db.query(Network).filter(Network.range_id == UUID(range_id)).all()
        vms = db.query(VM).filter(VM.range_id == UUID(range_id)).all()

        # Log deployment start event (needed for elapsed time calculation)
        event_service = EventService(db)
        event_service.log_event(
            range_id=UUID(range_id),
            event_type=EventType.DEPLOYMENT_STARTED,
            message=f"Starting DinD deployment of range '{range_obj.name}'",
            extra_data=json.dumps(
                {"total_networks": len(networks), "total_vms": len(vms), "isolation": "dind"}
            ),
        )

        # Run async deployment synchronously
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(deployment_service.deploy_range(db, UUID(range_id)))
            logger.info(f"Range {range_id} deployed successfully: {result}")

            # Log deployment completion
            event_service.log_event(
                range_id=UUID(range_id),
                event_type=EventType.DEPLOYMENT_COMPLETED,
                message=f"Range '{range_obj.name}' deployed successfully with DinD isolation",
                extra_data=json.dumps(
                    {
                        "networks_deployed": len(networks),
                        "vms_deployed": len(vms),
                        "dind_container_id": range_obj.dind_container_id,
                    }
                ),
            )
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"Failed to deploy range {range_id}: {e}")
        range_obj = db.query(Range).filter(Range.id == UUID(range_id)).first()
        if range_obj:
            range_obj.status = RangeStatus.ERROR
            range_obj.error_message = str(e)[:1000]
            db.commit()

            # Log deployment failure
            try:
                event_service = EventService(db)
                event_service.log_event(
                    range_id=UUID(range_id),
                    event_type=EventType.DEPLOYMENT_FAILED,
                    message=f"Deployment failed: {str(e)[:500]}",
                )
            except Exception:
                pass
    finally:
        db.close()


@dramatiq.actor(max_retries=3, min_backoff=1000)
def deploy_range_task_legacy(range_id: str):
    """
    Legacy deployment task (non-DinD).
    Creates VyOS router, Docker networks, and starts all VMs directly on host.

    DEPRECATED: Use deploy_range_task() which uses DinD isolation.
    """
    logger.info(f"Starting legacy deployment for range {range_id}")

    db = get_session_local()()
    try:
        from proving_ground.services.docker_service import get_docker_service
        from proving_ground.services.vyos_service import get_vyos_service

        docker = get_docker_service()
        vyos = get_vyos_service()

        range_obj = db.query(Range).filter(Range.id == UUID(range_id)).first()
        if not range_obj:
            logger.error(f"Range {range_id} not found")
            return

        # Set to deploying
        range_obj.status = RangeStatus.DEPLOYING
        db.commit()

        # Step 0: Create VyOS router for this range
        router = db.query(RangeRouter).filter(RangeRouter.range_id == UUID(range_id)).first()
        if not router:
            # Allocate management IP and create router record
            management_ip = vyos.allocate_management_ip()
            router = RangeRouter(
                range_id=UUID(range_id), management_ip=management_ip, status=RouterStatus.CREATING
            )
            db.add(router)
            db.commit()

        try:
            if not router.container_id:
                # Create and start the VyOS router
                container_id = vyos.create_router_container(range_id, router.management_ip)
                router.container_id = container_id
                db.commit()

                vyos.start_router(container_id)

                # Wait for router to be ready
                if vyos.wait_for_router_ready(container_id, timeout=120):
                    router.status = RouterStatus.RUNNING
                else:
                    router.status = RouterStatus.ERROR
                    router.error_message = "Router failed to become ready"
                db.commit()

                # Connect traefik to management network for routing
                docker.connect_traefik_to_management_network()

            logger.info(f"VyOS router ready for range {range_id}")
        except Exception as e:
            logger.error(f"Failed to create VyOS router for range {range_id}: {e}")
            router.status = RouterStatus.ERROR
            router.error_message = str(e)[:500]
            db.commit()
            # Continue anyway - VMs can still work without VyOS features

        # Step 1: Provision all networks
        networks = db.query(Network).filter(Network.range_id == UUID(range_id)).all()
        interface_num = 1  # eth0 is management, start from eth1

        for network in networks:
            if not network.docker_network_id:
                # Networks are NOT internal when using VyOS - VyOS handles isolation
                docker_network_id = docker.create_network(
                    name=f"pg-{network.name}-{str(network.id)[:8]}",
                    subnet=network.subnet,
                    gateway=network.gateway,
                    internal=False,  # VyOS handles isolation, not Docker
                    labels={
                        "pg.range_id": range_id,
                        "pg.network_id": str(network.id),
                    },
                )
                network.docker_network_id = docker_network_id
                db.commit()

                # Connect traefik to this network for VNC/web console routing
                docker.connect_traefik_to_network(docker_network_id)

                # Connect VyOS router to this network
                if router and router.container_id and router.status == RouterStatus.RUNNING:
                    interface_name = f"eth{interface_num}"
                    network.vyos_interface = interface_name

                    # Router gets the gateway IP on this network
                    vyos.connect_to_network(router.container_id, docker_network_id, network.gateway)

                    # Configure the interface on VyOS
                    subnet_bits = network.subnet.split("/")[1]
                    vyos.configure_interface(
                        router.container_id,
                        interface_name,
                        f"{network.gateway}/{subnet_bits}",
                        description=network.name,
                    )

                    # Configure NAT if internet is enabled
                    if network.internet_enabled:
                        rule_num = interface_num * 10
                        vyos.configure_nat_outbound(router.container_id, rule_num, network.subnet)

                    # Configure firewall for isolation
                    if network.is_isolated and not network.internet_enabled:
                        vyos.configure_firewall_isolated(router.container_id, interface_name)

                    # Configure DHCP server if enabled
                    if network.dhcp_enabled:
                        vyos.configure_dhcp_server(
                            container_id=router.container_id,
                            network_name=network.name,
                            subnet=network.subnet,
                            gateway=network.gateway,
                            dns_servers=network.dns_servers,
                            dns_search=network.dns_search,
                        )

                    interface_num += 1
                    db.commit()

                logger.info(
                    f"Provisioned network {network.name} (isolated={network.is_isolated}, internet={network.internet_enabled}, dhcp={network.dhcp_enabled})"
                )

        # Step 2: Create and start all VMs
        vms = db.query(VM).filter(VM.range_id == UUID(range_id)).all()
        for vm in vms:
            try:
                if vm.container_id:
                    docker.start_container(vm.container_id)
                else:
                    network = db.query(Network).filter(Network.id == vm.network_id).first()
                    # Load image sources (base_image, golden_image, or snapshot)
                    base_img = (
                        db.query(BaseImage).filter(BaseImage.id == vm.base_image_id).first()
                        if vm.base_image_id
                        else None
                    )
                    golden_img = (
                        db.query(GoldenImage).filter(GoldenImage.id == vm.golden_image_id).first()
                        if vm.golden_image_id
                        else None
                    )
                    snapshot = (
                        db.query(Snapshot).filter(Snapshot.id == vm.snapshot_id).first()
                        if vm.snapshot_id
                        else None
                    )

                    if not network or not network.docker_network_id:
                        logger.warning(f"Skipping VM {vm.id}: network not provisioned")
                        continue

                    # Determine image properties from source
                    if base_img:
                        image_ref = base_img.docker_image_tag or ""
                        os_type = base_img.os_type
                        vm_type_str = base_img.vm_type
                    elif golden_img:
                        image_ref = golden_img.docker_image_tag or ""
                        os_type = golden_img.os_type
                        vm_type_str = golden_img.vm_type
                    elif snapshot:
                        image_ref = snapshot.docker_image_id or ""
                        os_type = snapshot.os_type or "linux"
                        vm_type_str = snapshot.vm_type or "container"
                    else:
                        logger.warning(f"Skipping VM {vm.id}: no image source found")
                        continue

                    labels = {
                        "pg.range_id": range_id,
                        "pg.vm_id": str(vm.id),
                        "pg.hostname": vm.hostname,
                    }

                    # Add Traefik labels for VNC web console routing
                    # This is needed for async deployments (blueprints, instances)
                    display_type = vm.display_type or "desktop"
                    if display_type == "desktop":
                        vm_id_short = str(vm.id).replace("-", "")[:16]
                        is_linuxserver = (
                            "linuxserver/" in image_ref or "lscr.io/linuxserver" in image_ref
                        )
                        is_kasmweb = "kasmweb/" in image_ref

                        # Determine VNC port and scheme based on image type
                        # QEMU-based VMs (Linux VM, Windows, custom ISO) use port 8006
                        is_qemu_vm = (
                            vm_type_str == "linux_vm"
                            or vm_type_str == "windows_vm"
                            or os_type == "windows"
                            or os_type == "custom"
                            or image_ref.startswith("iso:")
                        )
                        if is_qemu_vm:
                            vnc_port = "8006"
                            vnc_scheme = "http"
                            needs_auth = False
                        elif is_linuxserver:
                            vnc_port = "3000"
                            vnc_scheme = "http"
                            needs_auth = False
                        elif is_kasmweb:
                            vnc_port = "6901"
                            vnc_scheme = "https"
                            needs_auth = True
                        else:
                            vnc_port = "6901"
                            vnc_scheme = "https"
                            needs_auth = False

                        router_name = f"vnc-{vm_id_short}"
                        middlewares = [f"vnc-strip-{vm_id_short}"]
                        range_network_name = f"pg-{network.name}-{str(network.id)[:8]}"

                        labels.update(
                            {
                                "traefik.enable": "true",
                                "traefik.docker.network": range_network_name,
                                # Service
                                f"traefik.http.services.{router_name}.loadbalancer.server.port": vnc_port,
                                f"traefik.http.services.{router_name}.loadbalancer.server.scheme": vnc_scheme,
                                # HTTP router
                                f"traefik.http.routers.{router_name}.rule": f"PathPrefix(`/vnc/{vm.id}`)",
                                f"traefik.http.routers.{router_name}.entrypoints": "web",
                                f"traefik.http.routers.{router_name}.service": router_name,
                                f"traefik.http.routers.{router_name}.priority": "100",
                                # HTTPS router
                                f"traefik.http.routers.{router_name}-secure.rule": f"PathPrefix(`/vnc/{vm.id}`)",
                                f"traefik.http.routers.{router_name}-secure.entrypoints": "websecure",
                                f"traefik.http.routers.{router_name}-secure.tls": "true",
                                f"traefik.http.routers.{router_name}-secure.service": router_name,
                                f"traefik.http.routers.{router_name}-secure.priority": "100",
                                # Strip prefix middleware
                                f"traefik.http.middlewares.vnc-strip-{vm_id_short}.stripprefix.prefixes": f"/vnc/{vm.id}",
                            }
                        )

                        if vnc_scheme == "https":
                            labels[
                                f"traefik.http.services.{router_name}.loadbalancer.serversTransport"
                            ] = "insecure-transport@file"

                        if needs_auth:
                            auth_string = base64.b64encode(b"kasm_user:vncpassword").decode()
                            auth_middleware = f"vnc-auth-{vm_id_short}"
                            labels[
                                f"traefik.http.middlewares.{auth_middleware}.headers.customrequestheaders.Authorization"
                            ] = f"Basic {auth_string}"
                            middlewares.append(auth_middleware)

                        labels[f"traefik.http.routers.{router_name}.middlewares"] = ",".join(
                            middlewares
                        )
                        labels[f"traefik.http.routers.{router_name}-secure.middlewares"] = ",".join(
                            middlewares
                        )

                    if os_type == "windows":
                        # Resolve Windows version from VM or image
                        win_version = vm.windows_version
                        if not win_version and image_ref:
                            # Extract version from image_ref like "dockurr/windows:2022" or just "2022"
                            if ":" in image_ref:
                                win_version = image_ref.split(":")[-1]
                            elif image_ref.replace(".", "").isdigit() or image_ref in [
                                "11",
                                "10",
                                "2025",
                                "2022",
                                "2019",
                                "2016",
                                "2012",
                                "2008",
                            ]:
                                win_version = image_ref
                        if not win_version:
                            win_version = "11"  # Default to Windows 11

                        logger.info(
                            f"Creating Windows VM {vm.hostname} with version: {win_version}"
                        )
                        container_id = docker.create_windows_container(
                            name=f"pg-{vm.hostname}-{str(vm.id)[:8]}",
                            network_id=network.docker_network_id,
                            ip_address=vm.ip_address,
                            cpu_limit=vm.cpu,
                            memory_limit_mb=vm.ram_mb,
                            disk_size_gb=vm.disk_gb,
                            windows_version=win_version,
                            labels=labels,
                            gateway=network.gateway,
                            dns_servers=network.dns_servers,
                            dns_search=network.dns_search,
                        )
                    elif vm_type_str == "linux_vm":
                        # Linux VM using qemux/qemu
                        settings = get_settings()
                        vm_storage_path = os.path.join(
                            settings.vm_storage_dir, str(vm.range_id), str(vm.id), "storage"
                        )
                        linux_distro = vm.linux_distro or "ubuntu"
                        boot_mode = vm.boot_mode or "uefi"
                        disk_type = vm.disk_type or "scsi"
                        iso_path = base_img.iso_path if base_img and base_img.iso_path else None

                        logger.info(f"Creating Linux VM {vm.hostname} with distro: {linux_distro}")
                        container_id = docker.create_linux_vm_container(
                            name=f"pg-{vm.hostname}-{str(vm.id)[:8]}",
                            network_id=network.docker_network_id,
                            ip_address=vm.ip_address,
                            cpu_limit=vm.cpu,
                            memory_limit_mb=vm.ram_mb,
                            disk_size_gb=vm.disk_gb,
                            linux_distro=linux_distro,
                            labels=labels,
                            iso_path=iso_path,
                            storage_path=vm_storage_path,
                            boot_mode=boot_mode,
                            disk_type=disk_type,
                            display_type=vm.display_type or "desktop",
                            gateway=network.gateway,
                            dns_servers=network.dns_servers,
                            dns_search=network.dns_search,
                        )
                    elif os_type == "custom" or image_ref.startswith("iso:"):
                        # Custom ISO or ISO-based Linux VMs use qemux/qemu
                        settings = get_settings()
                        vm_storage_path = os.path.join(
                            settings.vm_storage_dir, str(vm.range_id), str(vm.id), "storage"
                        )
                        if os_type == "custom":
                            linux_distro = "custom"
                            iso_path = base_img.iso_path if base_img and base_img.iso_path else None
                        else:
                            linux_distro = image_ref.replace("iso:", "")
                            iso_path = None

                        logger.info(
                            f"Creating custom/ISO VM {vm.hostname} with distro: {linux_distro}"
                        )
                        container_id = docker.create_linux_vm_container(
                            name=f"pg-{vm.hostname}-{str(vm.id)[:8]}",
                            network_id=network.docker_network_id,
                            ip_address=vm.ip_address,
                            cpu_limit=vm.cpu,
                            memory_limit_mb=vm.ram_mb,
                            disk_size_gb=vm.disk_gb,
                            linux_distro=linux_distro,
                            labels=labels,
                            iso_path=iso_path,
                            storage_path=vm_storage_path,
                            display_type=vm.display_type or "desktop",
                            gateway=network.gateway,
                            dns_servers=network.dns_servers,
                            dns_search=network.dns_search,
                        )
                    else:
                        # Docker container (Samba DC, linuxserver images, etc.)
                        needs_privileged = "samba-dc" in image_ref
                        container_id = docker.create_container(
                            name=f"pg-{vm.hostname}-{str(vm.id)[:8]}",
                            image=image_ref,
                            network_id=network.docker_network_id,
                            ip_address=vm.ip_address,
                            cpu_limit=vm.cpu,
                            memory_limit_mb=vm.ram_mb,
                            hostname=vm.hostname,
                            labels=labels,
                            dns_servers=network.dns_servers,
                            dns_search=network.dns_search,
                            privileged=needs_privileged,
                        )

                    vm.container_id = container_id
                    docker.start_container(container_id)

                vm.status = VMStatus.RUNNING
                db.commit()
                logger.info(f"Started VM {vm.hostname}")

            except Exception as e:
                logger.error(f"Failed to start VM {vm.id}: {e}")
                vm.status = VMStatus.ERROR
                db.commit()

        range_obj.status = RangeStatus.RUNNING
        db.commit()
        logger.info(f"Range {range_id} deployed successfully")

    except Exception as e:
        logger.error(f"Failed to deploy range {range_id}: {e}")
        range_obj = db.query(Range).filter(Range.id == UUID(range_id)).first()
        if range_obj:
            range_obj.status = RangeStatus.ERROR
            db.commit()
    finally:
        db.close()


@dramatiq.actor(max_retries=3, min_backoff=1000)
def teardown_range_task(range_id: str):
    """
    Async task to teardown a range.
    Stops and removes all VMs, VyOS router, then removes networks.

    Dispatches on `settings.range_substrate` exactly as `deploy_range_task` does.
    """
    if settings.range_substrate == "kubernetes":
        _teardown_range_on_kubernetes(range_id)
        return

    logger.info(f"Starting async teardown for range {range_id}")

    db = get_session_local()()
    try:
        from proving_ground.services.docker_service import get_docker_service
        from proving_ground.services.vyos_service import get_vyos_service

        docker = get_docker_service()
        vyos = get_vyos_service()

        # Step 1: Stop and remove all VM containers
        vms = db.query(VM).filter(VM.range_id == UUID(range_id)).all()
        for vm in vms:
            if vm.container_id:
                try:
                    docker.remove_container(vm.container_id, force=True)
                except Exception as e:
                    logger.warning(f"Failed to remove container for VM {vm.id}: {e}")
                vm.container_id = None
                vm.status = VMStatus.PENDING
                db.commit()
                logger.info(f"Removed VM {vm.hostname}")

        # Step 2: Remove VyOS router
        router = db.query(RangeRouter).filter(RangeRouter.range_id == UUID(range_id)).first()
        if router and router.container_id:
            try:
                vyos.remove_router(router.container_id)
            except Exception as e:
                logger.warning(f"Failed to remove VyOS router: {e}")
            router.container_id = None
            router.status = RouterStatus.PENDING
            db.commit()
            logger.info(f"Removed VyOS router for range {range_id}")

        # Step 3: Remove all Docker networks
        networks = db.query(Network).filter(Network.range_id == UUID(range_id)).all()
        for network in networks:
            if network.docker_network_id:
                try:
                    # Disconnect traefik before deleting network
                    docker.disconnect_traefik_from_network(network.docker_network_id)
                    docker.delete_network(network.docker_network_id)
                except Exception as e:
                    logger.warning(f"Failed to delete network {network.id}: {e}")
                network.docker_network_id = None
                network.vyos_interface = None
                db.commit()
                logger.info(f"Removed network {network.name}")

        range_obj = db.query(Range).filter(Range.id == UUID(range_id)).first()
        if range_obj:
            range_obj.status = RangeStatus.DRAFT
            db.commit()

        logger.info(f"Range {range_id} torn down successfully")

    except Exception as e:
        logger.error(f"Failed to teardown range {range_id}: {e}")
    finally:
        db.close()


@dramatiq.actor(max_retries=0)
def start_range_task(range_id: str, user_id: str | None = None):
    """Start a Kubernetes range in the background.

    On the default queue rather than the throttled `deploys` one: a cohort starting their labs at
    the top of a session must not queue behind each other.

    No retries. The body records every failure on the range itself, so a retry would replace a
    recorded reason with a second attempt nobody asked for.
    """
    if settings.range_substrate != "kubernetes":
        # Era A starts synchronously in the endpoint, where its DinD work is bounded. Nothing
        # enqueues this there, and silently starting a DinD range from here would be a second
        # start path on a frozen one.
        logger.error("start_range_task is the Kubernetes path; this host is not on it")
        return
    _start_range_on_kubernetes(range_id, user_id)


@dramatiq.actor(max_retries=0)
def stop_range_task(range_id: str, user_id: str | None = None):
    """Stop a Kubernetes range in the background. See `start_range_task` for the queue and retries."""
    if settings.range_substrate != "kubernetes":
        logger.error("stop_range_task is the Kubernetes path; this host is not on it")
        return
    _stop_range_on_kubernetes(range_id, user_id)
