"""Deploy and destroy a range on the Kubernetes substrate -- Era B.

The Era A path (`range_deployment_service.deploy_range`) is untouched and frozen at v0.45.0. This
is a parallel path selected by `settings.range_substrate`, not a rewrite of that one; the two meet
only at the dispatch in `tasks/deployment.py`.

What a deploy does, in order: resolve the range's placement by policy; realise it as a namespace
with its isolation floor (and its vcluster, where placement says so); apply the range networks;
apply the workloads as KubeVirt VMs; install and seed every capability the blueprint declares;
then wait for the VMs and read back the addresses they came up with. Everything is applied before
anything is waited on, so a VM boots while its capability chart installs.

A failure anywhere is the deploy's failure. Era A collects partial failures and reports success;
that is named as a defect in the 2026-09-15 handoff and it is not being ported.
"""

from __future__ import annotations

import asyncio
import logging
import re
from functools import lru_cache
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from proving_ground.capability import placement_for_assignment
from proving_ground.capability.blueprint import RangeSpec, read_blueprint
from proving_ground.capability.flux import FluxChartInstaller
from proving_ground.capability.kubernetes_client import KubernetesApiClient
from proving_ground.capability.kubernetes_runtime import KubernetesRuntime
from proving_ground.capability.lifecycle import IngressConfig, RangeLifecycle
from proving_ground.capability.models import Isolation, Placement
from proving_ground.capability.runtime import CapabilitySpec
from proving_ground.config import get_settings
from proving_ground.utils.range_hosts import range_id_from_key as _range_id_from_key
from proving_ground.models.blueprint import RangeInstance
from proving_ground.models.range import Range, RangeStatus

logger = logging.getLogger(__name__)

__all__ = [
    "RangeDeployError",
    "deploy_range_on_kubernetes",
    "destroy_range_on_kubernetes",
    "placement_for_range",
    "start_range_on_kubernetes",
    "stop_range_on_kubernetes",
    "validate_range_for_kubernetes",
]

RANGE_ID_LABEL = "pg.range/id"

# `Range.error_message` is String(1000) and the worker truncates to the same. Anything longer is
# lost between here and the page that displays it, so the message is composed to fit.
_MESSAGE_LIMIT = 1000
_REASON_LIMIT = 400

# How long the failure message may spend asking the cluster what it still holds. The reads below
# run after a deploy has already failed, and the cluster is often the reason it failed -- an API
# server that accepts a connection and then answers nothing would otherwise hold the deploy open
# for as long as it liked, and the range would sit at DEPLOYING with nothing written on it, which
# is the exact failure this whole message exists to end.
_READBACK_BUDGET_SECONDS = 20.0


class RangeDeployError(RuntimeError):
    """A deploy that failed, carrying the words the person who pressed Deploy will be shown.

    The worker writes `str(exc)` onto `Range.error_message` and into the deployment-failed event,
    and the range page renders that string and nothing else. So this message is the entire user
    interface for a failed deploy: it names the step, what did come up, and whether anything is
    still on the cluster. Before it, an instructor got a raw Kubernetes error -- or, when the
    worker's own handler could not run, a range that said "deploying" until they gave up.
    """


def _lifecycle(kube: KubernetesApiClient) -> RangeLifecycle:
    settings = get_settings()
    names = [n.strip() for n in settings.control_plane_namespaces.split(",") if n.strip()]
    ingress = None
    controller = None
    if settings.range_ingress_class:
        ingress = IngressConfig(
            class_name=settings.range_ingress_class,
            authz_url=settings.range_ingress_authz_url,
            apps_host=apps_host(),
            path_prefix=settings.range_ingress_path_prefix.rstrip("/"),
            scheme=apps_scheme(),
        )
        labels = dict(
            kv.split("=", 1)
            for kv in settings.ingress_controller_pod_labels.split(",")
            if "=" in kv
        )
        controller = (settings.ingress_controller_namespace, labels)
    return RangeLifecycle(
        kube,
        control_plane_namespaces=names,
        ingress=ingress,
        ingress_controller=controller,
    )


def range_key(range_id: UUID) -> str:
    """The label that names a range in its applications' host: the range id, unabridged.

    A canonical UUID is already a legal DNS label -- 36 characters of lowercase hex and hyphens,
    well inside 63 -- so `<range-key>.<apps host>` needs neither shortening nor a table to look
    the range back up in.
    """
    return str(range_id)


# Defined in utils/range_hosts so pg-gateway can apply the same rule without importing this
# module, which would drag a Kubernetes client and the ORM into the proxy. Re-exported because
# `svc.range_id_from_key` is what the callers and tests already say.
range_id_from_key = _range_id_from_key


# A DNS name and nothing else. An operator who writes a URL here -- "https://apps.example.com"
# is the obvious mistake -- would otherwise get that string into an Ingress rule's `host`, which
# the cluster rejects; the range's whole deploy then fails in a worker's log rather than anywhere
# the operator is looking.
_APPS_HOST = re.compile(
    r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$"
)


@lru_cache(maxsize=8)
def _apps_host(configured: str) -> str:
    """Cached to warn once per value: the authoriser asks this on every request it answers."""
    if configured and not (len(configured) <= 216 and _APPS_HOST.match(configured)):
        logger.warning(
            "RANGE_APPS_HOST is not a DNS name (%r): publishing no range applications",
            configured,
        )
        return ""
    return configured


def apps_host() -> str:
    """The DNS suffix a range's applications answer beneath. Empty means none are published.

    A value that is not a DNS name publishes nothing, exactly as no value does -- the listing
    then names the setting, which is somewhere the operator will look. 216 is what a DNS name's
    253 leaves once the range's own label and the dot after it have taken 37.
    """
    return _apps_host(get_settings().range_apps_host.strip().strip(".").lower())


def apps_scheme() -> str:
    """How the browser reaches that host.

    The same switch as the console ticket's cookie, because it is the same problem: the ticket
    cookie is marked Secure there, and a Secure cookie set over http:// never comes back.
    """
    return "https" if get_settings().console_cookie_secure else "http"


def app_host_for(range_id: UUID) -> str:
    """The host one range's applications answer on."""
    return f"{range_key(range_id)}.{apps_host()}"


def placement_for_range(range_obj: Range, capabilities: list[CapabilitySpec]) -> Placement:
    """Adapt the range row to the capability layer's derivation."""
    try:
        return placement_for_assignment(
            range_id=range_obj.id,
            training_event_id=range_obj.training_event_id,
            assigned_to_user_id=range_obj.assigned_to_user_id,
            capabilities=capabilities,
        )
    except ValueError as exc:
        raise ValueError(f"range {range_obj.id}: {exc}") from exc


def _blueprint_config(db: Session, range_obj: Range) -> dict[str, Any]:
    instance = db.query(RangeInstance).filter(RangeInstance.range_id == range_obj.id).first()
    if instance is None or instance.blueprint is None:
        return {}
    return instance.blueprint.config or {}


def _range_and_blueprint(db: Session, range_id: UUID) -> tuple[Range, RangeSpec]:
    range_obj = db.query(Range).filter(Range.id == range_id).first()
    if range_obj is None:
        raise ValueError(f"range {range_id} not found")
    return range_obj, read_blueprint(_blueprint_config(db, range_obj))


def _deployable(db: Session, range_id: UUID) -> tuple[Range, RangeSpec, Placement]:
    range_obj, blueprint = _range_and_blueprint(db, range_id)
    if not blueprint.deployable_on_kubernetes:
        # An Era A blueprint on the Era B substrate. Refusing beats deploying an empty range that
        # looks successful -- the learner would get a namespace and nothing in it.
        #
        # The text is what an instructor is shown, so it says what to do rather than which issue
        # tracks the migration; the issue key it used to quote (MIG-2) meant nothing to them.
        raise ValueError(
            f"range {range_id} uses a v{blueprint.schema_version} (Era A) blueprint, which "
            "describes Docker containers rather than the machines this host deploys. This host "
            "runs ranges on Kubernetes, so it needs a v2 blueprint; deploy one of those, or ask "
            "whoever authored this blueprint to convert it."
        )
    placement = placement_for_range(range_obj, list(blueprint.capabilities))
    return range_obj, blueprint, placement


def validate_range_for_kubernetes(db: Session, range_id: UUID) -> Placement:
    """Everything the deploy would refuse, answered before it is dispatched.

    Synchronous and cluster-free, so an endpoint can turn a `ValueError` into a 400 instead of
    the worker recording it in an event log nobody is watching.
    """
    return _deployable(db, range_id)[2]


def _record_deploy_failure(db: Session, range_id: UUID, message: str) -> None:
    """Put the reason on the range row from inside the deploy, not only from the worker.

    The worker's own handler writes the same thing, but it has to re-query the range first, on a
    session the failure may have left in a rolled-back transaction -- and that query raises, the
    handler dies with it, and the range sits at DEPLOYING with nothing anywhere to say why. This
    rolls back before it reads, so the one piece of code that knows what went wrong is also the
    one that records it.
    """
    try:
        db.rollback()
        _write_deploy_failure(db, range_id, message)
        return
    except Exception:
        # The caller's session can be past saving -- CI found it in SQLAlchemy's 'prepared'
        # state, where no further SQL may be emitted on it at all, and the recorded reason
        # became the database's complaint instead of the deploy's. The reason is the only thing
        # standing between the user and a range that says DEPLOYING forever, so it gets a
        # session of its own rather than sharing the one that just failed.
        logger.warning(
            "could not record the deploy failure on range %s from the caller's session; "
            "retrying on a fresh one",
            range_id,
        )

    try:
        from proving_ground.database import get_session_local

        session = get_session_local()()
        try:
            _write_deploy_failure(session, range_id, message)
        finally:
            session.close()
    except Exception:
        # Losing the database too must not replace the deploy's own failure with a traceback
        # about the database: the caller is about to raise the real one.
        logger.exception("could not record the deploy failure on range %s", range_id)


def _write_deploy_failure(session: Session, range_id: UUID, message: str) -> None:
    range_obj = session.query(Range).filter(Range.id == range_id).first()
    if range_obj is None:
        return
    range_obj.status = RangeStatus.ERROR
    range_obj.error_message = message[:_MESSAGE_LIMIT]
    session.commit()


def _reason(exc: BaseException) -> str:
    """One line of why, from an exception that may have none.

    `TimeoutError()` stringifies to the empty string, which would render as "failed while waiting
    for the machines: ." -- the class name at least names the shape of the failure.
    """
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:_REASON_LIMIT] + ("..." if len(text) > _REASON_LIMIT else "")


async def _workload_summary(
    lifecycle: RangeLifecycle, blueprint: RangeSpec, placement: Placement
) -> str:
    """How much of the range came up, read back without waiting for the rest.

    "Three of four machines came up and db is Provisioning" is a different problem from "none of
    them did", and the instructor is the one who has to tell them apart. Read-back failures are
    swallowed: the cluster is already the thing that went wrong, and losing the explanation of
    the real failure to a second one would be the worse outcome.
    """
    if not blueprint.workloads:
        return ""
    try:
        states = await asyncio.wait_for(
            lifecycle.workload_states(blueprint.workloads, placement),
            timeout=_READBACK_BUDGET_SECONDS,
        )
    except Exception:
        logger.warning("could not read workload states in %s", placement.namespace, exc_info=True)
        return ""
    total = len(states)
    running = sum(1 for s in states if s.status == "Running")
    if running == total:
        return f"All {total} machine(s) are running."
    stuck = ", ".join(
        f"{spec.name} is {state.status}"
        for spec, state in zip(blueprint.workloads, states, strict=False)
        if state.status != "Running"
    )
    return f"{running} of {total} machine(s) came up ({stuck})."


async def _objects_remain(kube: KubernetesApiClient, placement: Placement) -> bool:
    """Whether the cluster is still holding this range -- asked, not assumed.

    Tracking this as a flag set once `create()` returned called every failure inside it "nothing
    was created", and `create()` makes the namespace first, then the default-deny policy, then a
    vcluster's control plane: a failure in the second or third step leaves objects behind that
    the flag would deny. An instructor told there is nothing to tear down does not tear it down,
    and the next deploy lands on top of what is there.

    A read that cannot be answered assumes there is something: tearing down a range that never
    got a namespace is a no-op, while an orphaned vcluster is a control plane nobody owns.
    """
    try:
        return await asyncio.wait_for(
            kube.namespace_exists(placement.namespace), timeout=_READBACK_BUDGET_SECONDS
        )
    except Exception:
        logger.warning("could not read namespace %s back", placement.namespace, exc_info=True)
        return True


async def _deploy_failure_message(
    lifecycle: RangeLifecycle,
    blueprint: RangeSpec,
    placement: Placement,
    *,
    stage: str,
    installed: list[str],
    objects_remain: bool,
    exc: BaseException,
) -> str:
    """The whole of what a failed deploy tells its user, in the order they need it.

    Which step failed and why; how much of the range exists anyway; and whether the cluster is
    still holding objects, because a half-built range has to be torn down before the next attempt
    and nothing else on the page says so.
    """
    # A Kubernetes error rarely ends in a full stop, and the sentences that follow it run into
    # it when it does not.
    opening = f"Deploy failed while {stage}: {_reason(exc)}"
    parts = [opening if opening.endswith(".") else opening + "."]
    summary = await _workload_summary(lifecycle, blueprint, placement)
    if summary:
        parts.append(summary)
    if installed:
        parts.append(f"Capabilities already installed: {', '.join(installed)}.")
    if objects_remain:
        parts.append(
            f"Objects for this range are still on the cluster in namespace "
            f"{placement.namespace}; tear the range down before deploying it again."
        )
    return " ".join(parts)[:_MESSAGE_LIMIT]


async def deploy_range_on_kubernetes(db: Session, range_id: UUID) -> dict[str, Any]:
    """Place the range, realise it, and return what the cluster says it became.

    Every way out of here that is not the return statement leaves the range in ERROR with a
    reason written on the row, because that reason is the only signal the person who pressed
    Deploy gets: the endpoint answered before any of this ran.
    """
    try:
        range_obj, blueprint, placement = _deployable(db, range_id)
    except ValueError as exc:
        # Refused before the cluster was touched: nothing was built, so there is nothing to
        # describe beyond the refusal. Re-raised unchanged rather than wrapped, because the
        # endpoint that validates ahead of dispatch turns this same ValueError into a 400.
        _record_deploy_failure(db, range_id, str(exc))
        raise

    capabilities = list(blueprint.capabilities)
    logger.info(
        "range %s -> %s %s (%s)",
        range_id,
        placement.isolation.value,
        placement.namespace,
        placement.reason,
    )

    labels = {RANGE_ID_LABEL: str(range_id)}
    try:
        kube = await KubernetesApiClient.connect()
    except Exception as exc:
        # Not the range's fault and not fixable by redeploying it, so it says so plainly rather
        # than reporting a machine or a chart as the thing that failed.
        _record_deploy_failure(
            db,
            range_id,
            f"Deploy failed before it started: this platform could not reach its Kubernetes "
            f"cluster ({_reason(exc)}). Nothing was created. This is a platform problem rather "
            f"than a problem with the range or its blueprint.",
        )
        raise

    lifecycle = _lifecycle(kube)
    target: KubernetesApiClient | None = None
    installed: list[str] = []
    stage = "creating the range's namespace"
    try:
        try:
            await lifecycle.create(placement, labels=labels)
            stage = "applying the range networks"
            networks = await lifecycle.apply_networks(blueprint.networks, placement)
            stage = "creating the range's machines"
            await lifecycle.apply_workloads(
                blueprint.workloads, blueprint.networks, placement, extra_labels=labels
            )
            # VMs boot in the host namespace while the vcluster (if any) comes up; then the
            # capability goes wherever placement says it lives.
            if placement.isolation is Isolation.VCLUSTER:
                stage = "waiting for the range's private cluster to be ready"
                target = await KubernetesApiClient.from_kubeconfig_dict(
                    await lifecycle.await_vcluster_ready(placement)
                )
            runtime = _runtime(kube, target)
            for spec in capabilities:
                stage = f"installing the {spec.name} capability"
                await runtime.install(spec, placement)
                stage = f"seeding the {spec.name} capability"
                seeded = await runtime.seed(spec, placement)
                if not seeded.succeeded:
                    raise RuntimeError(f"seeding {spec.name} failed: {seeded.logs[-300:]}")
                installed.append(spec.name)
            # Routes only when the profile has an ingress class AND a host of its own to put them
            # on; a capability with a web surface on a profile with neither is not an error, it is
            # simply not published. `apps_of` is what tells the learner which of the two is
            # missing.
            routes: dict[str, str] = {}
            if any(c.web for c in capabilities) and lifecycle.publishes_apps:
                stage = "publishing the range's applications"
                routes = await lifecycle.apply_app_ingress(
                    capabilities, placement, range_key=range_key(range_id)
                )
            stage = "waiting for the machines to come up"
            running = await lifecycle.await_workloads_running(blueprint.workloads, placement)
        except Exception as exc:
            message = await _deploy_failure_message(
                lifecycle,
                blueprint,
                placement,
                stage=stage,
                installed=installed,
                objects_remain=await _objects_remain(kube, placement),
                exc=exc,
            )
            _record_deploy_failure(db, range_id, message)
            # Wrapped so the worker records this message rather than the bare cluster error, and
            # so the two places a failure is written -- the row and the event log -- agree.
            raise RangeDeployError(message) from exc
    finally:
        if target is not None:
            await target.close()
        await kube.close()

    return {
        "isolation": placement.isolation.value,
        "namespace": placement.namespace,
        "vcluster": placement.vcluster,
        "reason": placement.reason,
        "networks": list(networks),
        "workloads": {
            w.name: {"status": w.status, "addresses": dict(w.addresses)} for w in running
        },
        "capabilities": installed,
        "apps": routes,
    }


def _runtime(host: KubernetesApiClient, target: KubernetesApiClient | None) -> KubernetesRuntime:
    """The one runtime, pointed at where the capability lives.

    Charts are always declared to the *host* -- the HelmRelease is reconciled by the host's Flux
    and, for a vcluster placement, deploys through the exported kubeconfig. Hooks and exec run
    where the capability's pods are: the vcluster when there is one, the host namespace
    otherwise. Same class, same behaviour; only the client differs (CLAUDE.md rule 2).
    """
    return KubernetesRuntime(target or host, chart_installer=FluxChartInstaller(host))


async def stop_range_on_kubernetes(db: Session, range_id: UUID) -> dict[str, Any]:
    """Scale the capabilities to zero and stop the VMs, keeping every disk."""
    _range_obj, _blueprint, placement = _deployable(db, range_id)
    kube = await KubernetesApiClient.connect()
    try:
        stopped = await _lifecycle(kube).stop(placement)
    finally:
        await kube.close()
    return {"namespace": placement.namespace, "stopped": stopped}


async def start_range_on_kubernetes(db: Session, range_id: UUID) -> dict[str, Any]:
    """Bring a stopped range back, and do not say so until its VMs are Running."""
    _range_obj, blueprint, placement = _deployable(db, range_id)
    kube = await KubernetesApiClient.connect()
    lifecycle = _lifecycle(kube)
    try:
        started = await lifecycle.start(placement)
        running = await lifecycle.await_workloads_running(blueprint.workloads, placement)
    finally:
        await kube.close()
    return {
        "namespace": placement.namespace,
        "started": started,
        "workloads": {
            w.name: {"status": w.status, "addresses": dict(w.addresses)} for w in running
        },
    }


async def destroy_range_on_kubernetes(db: Session, range_id: UUID) -> dict[str, Any]:
    """Tear the range down and prove it: residue is a failure, not a footnote.

    Placement is re-derived from the range row rather than read from a stored name, so this
    works on a range whose deploy never ran or failed halfway -- and it is why no column was
    added to record where the range went.
    """
    range_obj, blueprint = _range_and_blueprint(db, range_id)
    capabilities = list(blueprint.capabilities)
    placement = placement_for_range(range_obj, capabilities)

    kube = await KubernetesApiClient.connect()
    lifecycle = _lifecycle(kube)
    target: KubernetesApiClient | None = None
    try:
        # Capabilities first, and each waited for: a release still uninstalling from a vcluster
        # that has since been deleted is stranded on its finalizer, and the namespace with it.
        # A vcluster that never came up (a deploy that failed early) has nothing to uninstall.
        if placement.isolation is Isolation.VCLUSTER:
            kubeconfig = await lifecycle.vcluster_kubeconfig(placement)
            if kubeconfig is not None:
                target = await KubernetesApiClient.from_kubeconfig_dict(kubeconfig)
                runtime = _runtime(kube, target)
                for spec in capabilities:
                    await runtime.uninstall(spec, placement)
        await lifecycle.destroy(placement)
        residue = await lifecycle.residue(placement)
    finally:
        if target is not None:
            await target.close()
        await kube.close()

    if not residue.is_clean:
        raise RuntimeError(
            f"teardown of range {range_id} left residue in {placement.namespace}: "
            f"{residue.describe()}"
        )
    return {
        "isolation": placement.isolation.value,
        "namespace": placement.namespace,
        "vcluster": placement.vcluster,
        "residue": residue.describe(),
        "clean": residue.is_clean,
    }
