"""Range lifecycle across both placement resolutions -- COSMOS PG-41, PG-42, PG-58.

`placement.py` decides *where* a range goes. This creates, stops, starts and destroys it there,
realises its networks and workloads there (the `NetworkAttachmentDefinition`s and KubeVirt
`VirtualMachine`s that `networking.py` and `kubevirt.py` build), and answers the question the
acceptance criteria care most about: did the teardown actually leave nothing behind.

**No new columns.** Placement is derived deterministically from the range's existing fields, so
teardown re-derives it rather than reading a stored `vcluster_name`. That is what satisfies "range
state reflected in the `Range` model without Docker-specific fields" -- not by adding
substrate-neutral fields, but by needing none. `Range.dind_container_id` is the mistake being
unwound; adding `Range.vcluster_name` would be the same mistake in a new coat.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import yaml

from .flux import VCLUSTER_KUBECONFIG_KEY, VCLUSTER_KUBECONFIG_SECRET
from .kube import KubeClient
from .kubevirt import KUBEVIRT_API, virtual_machine_manifest
from .models import Isolation, Placement, to_dns_label
from .networking import (
    NetworkSpec,
    interface_attachment_definition,
    network_attachment_definition,
)
from .runtime import CapabilitySpec, ChartRef
from .workload import WorkloadSpec

logger = logging.getLogger(__name__)

#: The ingress class a range's published applications name. NO controller serves it: the object
#: is a declaration that an application exists, which pg-gateway reads to build its route table,
#: while the single wildcard Ingress the chart owns is what actually answers on the network.
#: Pointing this at a real controller would publish a range's application with no authorisation
#: in front of it.
GATEWAY_INGRESS_CLASS = "pg-gateway"

#: Which application, and which range, an Ingress is for -- so the gateway reads labels rather
#: than parsing the host and path back apart.
APP_LABEL = "pg.app/name"
RANGE_LABEL = "pg.range/id"

__all__ = [
    "EgressFloor",
    "IngressConfig",
    "PRIVATE_NETWORKS",
    "RangeLifecycle",
    "Residue",
    "VCLUSTER_CHART",
    "WorkloadState",
]


# Pinned. The OSS backing store is the default; do NOT enable controlPlane.backingStore.etcd.embedded
# -- that is a licensed feature and the pod CrashLoopBackOffs with "embedded etcd is not enabled for
# this license", which reads like a bug and is a billing decision.
VCLUSTER_CHART = ChartRef(name="vcluster", version="0.37.0", repository="https://charts.loft.sh")

_HELM_GROUP, _HELM_VERSION, _HELM_PLURAL = "helm.toolkit.fluxcd.io", "v2", "helmreleases"
_NAD_GROUP, _NAD_VERSION, _NAD_PLURAL = "k8s.cni.cncf.io", "v1", "network-attachment-definitions"
_KUBEVIRT_GROUP, _KUBEVIRT_VERSION = KUBEVIRT_API.split("/")
_VM_PLURAL, _VMI_PLURAL = "virtualmachines", "virtualmachineinstances"

# KubeVirt's `status.printableStatus` values that will not resolve on their own. Waiting out the
# timeout on one of these hides the reason behind "timed out"; the status *is* the reason.
_VM_TERMINAL = frozenset(
    {
        "CrashLoopBackOff",
        "DataVolumeError",
        "ErrImagePull",
        "ErrorDataVolumeNotFound",
        "ErrorPvcNotFound",
        "ErrorUnschedulable",
        "ImagePullBackOff",
    }
)


@dataclass(frozen=True, slots=True)
class IngressConfig:
    """How a range's application is put on the cluster's ingress -- COSMOS PG-62.

    `apps_host` is a bare DNS suffix and every range answers on a label of its own beneath it,
    `<range-key>.<apps_host>`. The separation is the whole point: the application is software
    the learner is being trained against, third party at best and hostile by design in a
    red-team exercise, and on the platform's own host its JavaScript is same-origin with the
    console -- it reads the operator's token out of `localStorage` and calls the API as whoever
    opened it. A host per range puts the browser's own origin boundary between the two. Empty
    publishes nothing, which is the correct answer for an install that has no second host.

    `path_prefix` is an optional sub-path beneath that host; empty -- the default -- puts each
    application at `/<app>`, because the host already says which range it belongs to.

    `authz_url` is the platform endpoint the ingress asks, per request, whether the browser
    holding the cookie may reach this range's application: authorisation on the data path,
    not only on the endpoint that minted the URL. Traefik's ForwardAuth is the mechanism; it is
    the one Traefik-shaped thing here, and the ingress class names it.

    `scheme` is what the browser reaches that host with, and the returned URLs are absolute
    because they no longer live on the origin the caller asked from.
    """

    class_name: str
    authz_url: str
    apps_host: str = ""
    path_prefix: str = ""
    scheme: str = "https"


@dataclass(frozen=True, slots=True)
class WorkloadState:
    """A running workload and the addresses it came up with -- evidence, not a status code."""

    name: str
    status: str
    addresses: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Residue:
    """What a teardown left behind. Empty means clean."""

    namespace: bool
    cluster_rbac: tuple[str, ...]
    persistent_volumes: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        return not (self.namespace or self.cluster_rbac or self.persistent_volumes)

    def describe(self) -> str:
        if self.is_clean:
            return "clean"
        parts = []
        if self.namespace:
            parts.append("namespace still present")
        if self.cluster_rbac:
            parts.append(f"cluster RBAC: {', '.join(self.cluster_rbac)}")
        if self.persistent_volumes:
            parts.append(f"volumes: {', '.join(self.persistent_volumes)}")
        return "; ".join(parts)


# The address ranges a cluster keeps itself in: RFC1918, the carrier-grade space AWS hands to
# secondary pod CIDRs, and link-local, which is where a cloud instance's metadata service lives.
# A range is denied all of it and allowed the rest of the internet, rather than the reverse,
# because this module is not told what this particular cluster's pod and service CIDRs are -- and
# a list that had to be configured correctly would silently allow everything wherever it was not.
PRIVATE_NETWORKS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
    "169.254.0.0/16",
)


@dataclass(frozen=True, slots=True)
class EgressFloor:
    """What a range may reach once it is running.

    A learner owns the guest OS of their machine by design, and in a red-team exercise that
    machine is hostile on purpose. Its pod interface is on the cluster's flat network, so with
    no egress rule it reaches the platform's own Postgres, MinIO, Redis and API by service name.
    Redis was given a policy of its own precisely because it is unauthenticated; the rest were
    left on the argument that they have passwords, which makes any credential leak immediately
    exploitable from inside a range.

    `dns_namespace` is where CoreDNS runs. Allowed on port 53 only, and by namespace rather than
    by pod label, because the labels differ between distributions ("k8s-app: kube-dns" on k3s)
    and a range that cannot resolve a name is a range that cannot start.

    `closed_networks` are the addresses a range may not reach at all. `extra_destinations` is
    for the install that has to reach something inside them -- an in-cluster image mirror on an
    air-gapped profile is the case this exists for.
    """

    dns_namespace: str = "kube-system"
    closed_networks: tuple[str, ...] = PRIVATE_NETWORKS
    extra_destinations: tuple[str, ...] = ()


def default_deny_policy(
    namespace: str,
    *,
    control_plane_namespaces: Sequence[str] = (),
    ingress_controller: tuple[str, Mapping[str, str]] | None = None,
    egress: EgressFloor | None = None,
) -> dict:
    """Deny ingress from outside the range, allow it from within -- and from the control plane.

    Kubernetes pod networking is flat: without this, a pod in one learner's range reaches a pod
    in another learner's range by IP, and nothing objects. Measured on k3s before this existed --
    a cross-range probe returned EXIT=0.

    One object carries both directions. Two policies would also work -- they are additive -- but
    a range's isolation floor being one object is what makes "the range has its floor" a thing a
    teardown check or an operator can see in a single `get`.

    `egress` is optional and the allow-list model for intended cross-range paths still belongs to
    RNET-2 (PG-59). Given one, a range may reach itself, DNS, and the internet outside the
    cluster's own address space; it may not reach the platform's namespace, the service network,
    or another range. Given none, egress stays open and the policy is ingress-only, which is what
    every install before this had.

    `control_plane_namespaces` are let in: PROVING GROUND's own pods run hooks against a
    vcluster's API server inside the range, and Flux's helm-controller deploys into it. Both are
    outside the range. Without this the floor keeps out the thing that built the range -- a
    vcluster placement fails with "connection refused" from the API pod, while a namespace with
    no policy answers. Which namespaces those are is the deployment's to say, not this module's.
    They are let *in* only: the control plane opens connections to a range, and a range needing
    to open one back to it is the thing being prevented.
    """
    allowed: list[dict] = [{"podSelector": {}}]
    for ns in control_plane_namespaces:
        allowed.append({"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ns}}})
    if ingress_controller:
        # The ingress controller's pods, and only those, may reach a range's application (PG-62).
        # Namespace and pod selector together, so "kube-system" does not become an allow-all.
        ic_namespace, ic_labels = ingress_controller
        allowed.append(
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ic_namespace}},
                "podSelector": {"matchLabels": dict(ic_labels)},
            }
        )
    spec: dict = {
        "podSelector": {},
        "policyTypes": ["Ingress"],
        # An empty `from` would deny everything including same-range traffic, which breaks
        # every multi-tier range. Same-namespace is the unit of trust.
        "ingress": [{"from": allowed}],
    }
    if egress is not None:
        spec["policyTypes"] = ["Ingress", "Egress"]
        spec["egress"] = _egress_rules(egress)
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "pg-range-default-deny", "namespace": namespace},
        "spec": spec,
    }


def _egress_rules(egress: EgressFloor) -> list[dict]:
    """The three destinations a range genuinely needs, and nothing else.

    Rules are a union, so the same-namespace rule keeps the range's own traffic working however
    the address rule below is written -- a range's pods carry addresses out of the cluster's pod
    CIDR, which the address rule excludes.

    Images are pulled by the kubelet and capability charts are fetched by Flux's
    source-controller, neither of which is a pod in this namespace, so neither needs an allowance
    here. A CDI importer *is*, and it is why the internet is left reachable: a boot disk is
    imported from a registry by a pod in the range's own namespace.
    """
    outside: list[dict] = [{"ipBlock": _cidr("0.0.0.0/0", egress.closed_networks)}]
    outside += [{"ipBlock": {"cidr": destination}} for destination in egress.extra_destinations]
    return [
        {"to": [{"podSelector": {}}]},
        {
            "to": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": egress.dns_namespace}
                    }
                }
            ],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
        },
        {"to": outside},
    ]


def _cidr(cidr: str, excluded: Sequence[str]) -> dict:
    """An `ipBlock`, carrying `except` only when there is something in it.

    An empty exception list reads, to whoever inspects the live policy, as an exception that was
    meant to be there and is not -- the policy this produces is one an operator has to be able to
    check against a real cluster.
    """
    return {"cidr": cidr, "except": list(excluded)} if excluded else {"cidr": cidr}


class RangeLifecycle:
    """Create, stop, start and destroy a range under either resolution."""

    def __init__(
        self,
        client: KubeClient,
        *,
        control_plane_namespaces: Sequence[str] = (),
        ingress: IngressConfig | None = None,
        ingress_controller: tuple[str, Mapping[str, str]] | None = None,
        egress: EgressFloor | None = EgressFloor(),
    ) -> None:
        self._kube = client
        self._control_plane = tuple(control_plane_namespaces)
        self._ingress = ingress
        self._ingress_controller = ingress_controller
        self._egress = egress

    def _egress_for(self, placement: Placement) -> EgressFloor | None:
        """The egress floor, or none for a vcluster range -- and it says which and why.

        A vcluster's syncer runs in the range's namespace and talks to the *host* cluster's API
        server to mirror objects onto it. By the time a NetworkPolicy is evaluated that
        destination is no longer `kubernetes.default`: kube-proxy has already translated it to a
        node address on a port neither this module nor the chart is told. Closing the cluster's
        own address space to a vcluster range would therefore stop it coming up at all, and it
        would look like the chart hanging rather than like a policy.

        So the range that needs it least -- a vcluster is already a control plane of its own --
        is the one that keeps open egress, and this is the line to delete once the deployment can
        name its API server's address.
        """
        if self._egress is None or placement.isolation is not Isolation.VCLUSTER:
            return self._egress
        logger.warning(
            "range %s is a vcluster placement: leaving egress open, because its syncer must "
            "reach the host cluster's API server at an address this deployment does not declare",
            placement.namespace,
        )
        return None

    @property
    def publishes_apps(self) -> bool:
        """Whether this profile has somewhere to put a range's applications at all."""
        return self._ingress is not None and bool(self._ingress.apps_host)

    async def create(
        self, placement: Placement, *, labels: Mapping[str, str] | None = None
    ) -> None:
        await self._kube.ensure_namespace(
            placement.namespace, {**self._labels(placement), **dict(labels or {})}
        )
        await self._kube.apply_network_policy(
            namespace=placement.namespace,
            body=default_deny_policy(
                placement.namespace,
                control_plane_namespaces=self._control_plane,
                ingress_controller=self._ingress_controller,
                egress=self._egress_for(placement),
            ),
        )
        if placement.isolation is Isolation.VCLUSTER:
            await self._create_vcluster(placement)

    async def apply_networks(
        self, networks: Sequence[NetworkSpec], placement: Placement
    ) -> tuple[str, ...]:
        """Each range network becomes a Multus attachment in the range's namespace."""
        for network in networks:
            await self._kube.apply_custom_object(
                group=_NAD_GROUP,
                version=_NAD_VERSION,
                plural=_NAD_PLURAL,
                namespace=placement.namespace,
                name=network.attachment_name,
                body=network_attachment_definition(network, placement.namespace),
            )
        return tuple(n.attachment_name for n in networks)

    async def apply_workloads(
        self,
        workloads: Sequence[WorkloadSpec],
        networks: Sequence[NetworkSpec],
        placement: Placement,
        *,
        extra_labels: Mapping[str, str] | None = None,
    ) -> tuple[str, ...]:
        """Each workload becomes its per-interface attachments and a KubeVirt `VirtualMachine`.

        Build everything, then apply everything. A workload naming an undeclared network, or an
        interface with no address, fails at manifest time, and it must fail before the first
        object lands -- a range with two of its three machines and an error is the Era A
        partial-failure shape, not something to port.

        The attachments precede the VM they belong to: they carry its addresses, and a launcher
        pod whose attachment does not exist yet fails at sandbox creation.
        """
        by_name = {n.name: n for n in networks}
        plan: list[tuple[str, dict]] = []
        for w in workloads:
            vm = virtual_machine_manifest(w, placement, networks, extra_labels=extra_labels)
            for interface in w.interfaces:
                plan.append(
                    (
                        _NAD_PLURAL,
                        interface_attachment_definition(
                            w, interface, by_name[interface.network], placement.namespace
                        ),
                    )
                )
            plan.append((_VM_PLURAL, vm))

        for plural, manifest in plan:
            group, version = manifest["apiVersion"].split("/")
            await self._kube.apply_custom_object(
                group=group,
                version=version,
                plural=plural,
                namespace=placement.namespace,
                name=manifest["metadata"]["name"],
                body=manifest,
            )
        return tuple(m["metadata"]["name"] for plural, m in plan if plural == _VM_PLURAL)

    async def apply_app_ingress(
        self, capabilities: Sequence[CapabilitySpec], placement: Placement, *, range_key: str
    ) -> dict[str, str]:
        """Each capability with a web surface gets a URL of its own on the range's own host.

        ONE object per application now, where there were three. It used to be a ForwardAuth
        middleware, a StripPrefix middleware, and an Ingress chaining them by annotation -- all
        `traefik.io/v1alpha1`, and therefore impossible on Azure Application Gateway, the AWS
        Load Balancer Controller, GKE's ingress or OpenShift's router, none of which has an
        external-authorisation middleware at all.

        Both steps moved into pg-gateway. What is left here is a plain Ingress naming a class no
        controller serves: a declaration that the application is published, which the gateway
        reads to build its route table. The object that actually answers on the network is the
        single wildcard Ingress the chart owns.

        Returns `{app: url}`, absolute, because the host is not the one the caller asked from.
        """
        if self._ingress is None:
            raise ValueError(
                "no ingress configured: the deployment profile must name an ingress class, the "
                "host range applications answer on and the authz endpoint before a range's "
                "applications can be routed"
            )
        cfg = self._ingress
        if not cfg.apps_host:
            # No second origin, so nothing is published. An application on the platform's own
            # host is same-origin with the console and reads the operator's session out of it;
            # an install that has not been given a host for its applications gets none, and the
            # range deploys without them rather than failing.
            logger.warning(
                "range %s declares web-facing capabilities but no applications host is "
                "configured (RANGE_APPS_HOST): publishing none of them",
                range_key,
            )
            return {}
        host = f"{range_key}.{cfg.apps_host}"
        routes: dict[str, str] = {}
        for spec in capabilities:
            if spec.web is None:
                continue
            app = to_dns_label(spec.name)
            base = to_dns_label("pg", "app", app)
            path = f"{cfg.path_prefix}/{app}"
            await self._kube.apply_ingress(
                namespace=placement.namespace,
                body={
                    "apiVersion": "networking.k8s.io/v1",
                    "kind": "Ingress",
                    "metadata": {
                        "name": base,
                        "namespace": placement.namespace,
                        "labels": {
                            "pg.app/name": app,
                            # Which range, so pg-gateway does not have to parse it back out of
                            # the host and cannot be moved into another range's table by a
                            # malformed one.
                            "pg.range/id": range_key,
                        },
                    },
                    "spec": {
                        # A class NO controller serves. This object is a declaration that the
                        # application is published, read by pg-gateway; the thing that actually
                        # answers on the network is the single wildcard Ingress the chart owns.
                        # Pointing this at a real controller would publish a range's application
                        # with no authorisation in front of it.
                        "ingressClassName": GATEWAY_INGRESS_CLASS,
                        "rules": [
                            {
                                "host": host,
                                "http": {
                                    "paths": [
                                        {
                                            "path": path,
                                            "pathType": "Prefix",
                                            "backend": {
                                                "service": {
                                                    "name": spec.web.service,
                                                    "port": {"number": spec.web.port},
                                                }
                                            },
                                        }
                                    ]
                                },
                            }
                        ],
                    },
                },
            )
            routes[app] = f"{cfg.scheme}://{host}{path}/"
        return routes

    async def await_workloads_running(
        self,
        workloads: Sequence[WorkloadSpec],
        placement: Placement,
        *,
        timeout_seconds: int = 600,
        poll_seconds: float = 3,
    ) -> tuple[WorkloadState, ...]:
        """Wait for every VM to reach `Running`, then read back its interfaces.

        The addresses come from the `VirtualMachineInstance`, which is what the cluster actually
        gave the guest -- the evidence for "the learner can reach 172.30.10.5" is that the VMI
        says so, not that the blueprint asked for it.
        """
        pending = {to_dns_label(w.name): w.name for w in workloads}
        last: dict[str, str] = {name: "not found yet" for name in pending}
        deadline = time.monotonic() + timeout_seconds
        states: dict[str, WorkloadState] = {}
        while pending:
            for name in list(pending):
                vm = await self._kube.get_custom_object(
                    group=_KUBEVIRT_GROUP,
                    version=_KUBEVIRT_VERSION,
                    plural=_VM_PLURAL,
                    namespace=placement.namespace,
                    name=name,
                )
                status = ((vm or {}).get("status") or {}).get("printableStatus")
                if status:
                    last[name] = status
                if status in _VM_TERMINAL:
                    raise RuntimeError(
                        f"workload {pending[name]!r} in {placement.namespace} failed: {status}"
                    )
                if status == "Running":
                    states[name] = WorkloadState(
                        name=name, status=status, addresses=await self._addresses(name, placement)
                    )
                    del pending[name]
            if pending and time.monotonic() >= deadline:
                waiting = ", ".join(f"{pending[n]}: {last[n]}" for n in pending)
                warnings = await self._kube.recent_warnings(namespace=placement.namespace)
                detail = ("; recent warnings: " + " | ".join(warnings)) if warnings else ""
                raise TimeoutError(
                    f"workloads in {placement.namespace} did not reach Running: {waiting}{detail}"
                )
            if pending:
                await asyncio.sleep(poll_seconds)
        return tuple(states[to_dns_label(w.name)] for w in workloads)

    async def workload_states(
        self, workloads: Sequence[WorkloadSpec], placement: Placement
    ) -> tuple[WorkloadState, ...]:
        """What each VM is doing right now, without waiting for anything.

        `status` is KubeVirt's `printableStatus`, or `absent` for a VM that was never created
        (a range that failed before it, or one torn down). Addresses only when Running.
        """
        states = []
        for w in workloads:
            name = to_dns_label(w.name)
            vm = await self._kube.get_custom_object(
                group=_KUBEVIRT_GROUP,
                version=_KUBEVIRT_VERSION,
                plural=_VM_PLURAL,
                namespace=placement.namespace,
                name=name,
            )
            status = ((vm or {}).get("status") or {}).get("printableStatus") if vm else "absent"
            status = status or "Unknown"
            addresses = await self._addresses(name, placement) if status == "Running" else {}
            states.append(WorkloadState(name=name, status=status, addresses=addresses))
        return tuple(states)

    async def _addresses(self, name: str, placement: Placement) -> dict[str, str]:
        vmi = await self._kube.get_custom_object(
            group=_KUBEVIRT_GROUP,
            version=_KUBEVIRT_VERSION,
            plural=_VMI_PLURAL,
            namespace=placement.namespace,
            name=name,
        )
        interfaces = ((vmi or {}).get("status") or {}).get("interfaces") or []
        return {
            i["name"]: i["ipAddress"] for i in interfaces if i.get("name") and i.get("ipAddress")
        }

    async def stop(self, placement: Placement) -> int:
        """Scale everything to zero, keeping state.

        Returns how many workloads were scaled, so a caller can tell "stopped a running range"
        from "stopped something that was already down" -- the two look identical otherwise, and
        conflating them is how a failed deploy gets reported as a successful stop.

        Both kinds of workload: the capability's pods scale to zero, the range's VMs are set
        not-running. Their disks stay -- that is what makes this a stop and not a destroy.
        """
        pods = await self._kube.scale_workloads(namespace=placement.namespace, replicas=0)
        vms = await self._kube.set_virtual_machines_running(
            namespace=placement.namespace, running=False
        )
        return pods + vms

    async def start(self, placement: Placement) -> int:
        pods = await self._kube.scale_workloads(namespace=placement.namespace, replicas=1)
        vms = await self._kube.set_virtual_machines_running(
            namespace=placement.namespace, running=True
        )
        return pods + vms

    async def destroy(
        self,
        placement: Placement,
        *,
        release_timeout: int = 180,
        namespace_timeout: int = 300,
        volume_timeout: int = 120,
        poll_seconds: float = 2,
    ) -> None:
        """Delete the release, then the namespace, then wait for both it and its volumes to go.

        **Order matters, and getting it wrong is silent.** The vcluster chart creates
        cluster-scoped RBAC (`vc-<release>-v-<namespace>`) which a namespace delete cannot remove;
        only helm-controller's uninstall does. Deleting the namespace first races that uninstall --
        the HelmRelease disappears with its namespace before the controller has finished, and the
        ClusterRole and ClusterRoleBinding are stranded forever. Observed on k3s: a namespace-first
        teardown left both behind, and nothing reported an error.

        So: delete the HelmRelease, wait for the controller to finish, then delete the namespace.

        And then wait for it: a namespace delete returns at once and the namespace sits in
        `Terminating` while its VMs and PVCs are reaped. A `residue()` taken at that instant says
        "namespace still present" for every teardown, which makes the check worthless.

        The volumes need the same wait, for a reason only a real cloud disk shows. A PV outlives
        the namespace whose claim bound it: the namespace goes once its PVC objects are gone, but
        the PV is cluster-scoped and stays `Released` until the CSI driver has deleted the disk
        behind it. Measured on AKS with `disk.csi.azure.com`: namespace gone at 21s, PV gone at
        34s. `residue()` run in that 13-second window finds a PV whose claimRef names the
        namespace, calls the teardown dirty, and `DELETE /ranges/{id}` answers 502 and keeps the
        row -- for a teardown that was in fact about to finish cleanly.

        k3s cannot show this: `local-path` PVs are host directories the provisioner reaps with
        the namespace, so the window is too small to lose a race in. Every Proxmox teardown has
        passed for that reason and every AKS one failed.
        """
        if placement.isolation is Isolation.VCLUSTER:
            await self._kube.delete_custom_object(
                group=_HELM_GROUP,
                version=_HELM_VERSION,
                plural=_HELM_PLURAL,
                namespace=placement.namespace,
                name="vcluster",
            )
            await self._await_release_gone(placement, timeout_seconds=release_timeout)
        await self._kube.delete_namespace(placement.namespace)
        await self._await_namespace_gone(
            placement, timeout_seconds=namespace_timeout, poll_seconds=poll_seconds
        )
        await self._await_volumes_gone(
            placement, timeout_seconds=volume_timeout, poll_seconds=poll_seconds
        )

    async def _await_namespace_gone(
        self, placement: Placement, *, timeout_seconds: int, poll_seconds: float
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not await self._kube.namespace_exists(placement.namespace):
                return
            await asyncio.sleep(poll_seconds)
        # Not fatal for the same reason as the release: residue() will say so, and a namespace
        # stuck Terminating usually names its finalizer, which is more useful than an exception.
        logger.warning(
            "namespace %s still terminating after %ss; check residue()",
            placement.namespace,
            timeout_seconds,
        )

    async def _await_volumes_gone(
        self, placement: Placement, *, timeout_seconds: int, poll_seconds: float
    ) -> None:
        """Wait for the PVs the namespace's claims bound to go with it.

        A PV that is genuinely stranded -- a `Retain` reclaim policy, a driver that cannot delete
        the disk -- never goes, and waiting the full timeout costs a teardown that was going to
        be reported dirty anyway. A PV that is merely slow goes in seconds. Only the second kind
        is common, so the wait is worth its cost.

        Not fatal on timeout, for the same reason as the namespace: `residue()` is the thing that
        reports, and it runs next.
        """
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            remaining = await self._kube.list_persistent_volumes_matching(placement.namespace)
            if not remaining:
                return
            await asyncio.sleep(poll_seconds)
        logger.warning(
            "volumes for %s still present after %ss; check residue()",
            placement.namespace,
            timeout_seconds,
        )

    async def vcluster_kubeconfig(self, placement: Placement) -> dict | None:
        """The vcluster's exported kubeconfig, or None while there is no vcluster yet."""
        secret = await self._kube.get_secret(
            namespace=placement.namespace, name=VCLUSTER_KUBECONFIG_SECRET
        )
        if not secret or VCLUSTER_KUBECONFIG_KEY not in secret:
            return None
        return yaml.safe_load(secret[VCLUSTER_KUBECONFIG_KEY])

    async def await_vcluster_ready(
        self, placement: Placement, *, timeout_seconds: int = 600, poll_seconds: float = 3
    ) -> dict:
        """Wait for the vcluster's release to be Ready and its kubeconfig to be exported.

        Returns the kubeconfig, which is what the runtime needs to run hooks *inside* the
        vcluster and what helm-controller reads to deploy into it.
        """
        if not placement.vcluster:
            raise ValueError(f"placement {placement.namespace} has no vcluster to wait for")
        deadline = time.monotonic() + timeout_seconds
        last = "no status yet"
        while True:
            obj = await self._kube.get_custom_object(
                group=_HELM_GROUP,
                version=_HELM_VERSION,
                plural=_HELM_PLURAL,
                namespace=placement.namespace,
                name="vcluster",
            )
            ready = False
            for condition in ((obj or {}).get("status") or {}).get("conditions", []):
                if condition.get("type") == "Ready":
                    ready = condition.get("status") == "True"
                    last = f"{condition.get('reason')}: {condition.get('message')}"
            if ready:
                kubeconfig = await self.vcluster_kubeconfig(placement)
                if kubeconfig is not None:
                    return kubeconfig
                last = "release Ready, kubeconfig secret not exported yet"
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"vcluster in {placement.namespace} did not become ready "
                    f"(no kubeconfig): {last}"
                )
            await asyncio.sleep(poll_seconds)

    async def _await_release_gone(self, placement: Placement, *, timeout_seconds: int) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            existing = await self._kube.get_custom_object(
                group=_HELM_GROUP,
                version=_HELM_VERSION,
                plural=_HELM_PLURAL,
                namespace=placement.namespace,
                name="vcluster",
            )
            if existing is None:
                return
            await asyncio.sleep(2)
        # Not fatal: proceed to the namespace delete and let residue() report what was stranded,
        # which is more useful than refusing to tear down at all.
        logger.warning(
            "vcluster release in %s did not finish uninstalling; check residue()",
            placement.namespace,
        )

    async def residue(self, placement: Placement) -> Residue:
        """What is left after `destroy`. The point of this method is to be run and believed."""
        return Residue(
            namespace=await self._kube.namespace_exists(placement.namespace),
            cluster_rbac=tuple(await self._kube.cluster_rbac_matching(placement.namespace)),
            persistent_volumes=tuple(
                await self._kube.list_persistent_volumes_matching(placement.namespace)
            ),
        )

    # ------------------------------------------------------------------ internals

    async def _create_vcluster(self, placement: Placement) -> None:
        assert placement.vcluster  # guaranteed by Placement.__post_init__
        repo = "loft"
        await self._kube.apply_custom_object(
            group="source.toolkit.fluxcd.io",
            version="v1",
            plural="helmrepositories",
            namespace=placement.namespace,
            name=repo,
            body={
                "apiVersion": "source.toolkit.fluxcd.io/v1",
                "kind": "HelmRepository",
                "metadata": {"name": repo, "namespace": placement.namespace},
                "spec": {"interval": "30m", "url": VCLUSTER_CHART.repository},
            },
        )
        await self._kube.apply_custom_object(
            group=_HELM_GROUP,
            version=_HELM_VERSION,
            plural=_HELM_PLURAL,
            namespace=placement.namespace,
            name="vcluster",
            body={
                "apiVersion": f"{_HELM_GROUP}/{_HELM_VERSION}",
                "kind": "HelmRelease",
                "metadata": {"name": "vcluster", "namespace": placement.namespace},
                "spec": {
                    "interval": "5m",
                    "chart": {
                        "spec": {
                            "chart": VCLUSTER_CHART.name,
                            "version": VCLUSTER_CHART.version,
                            "sourceRef": {
                                "kind": "HelmRepository",
                                "name": repo,
                                "namespace": placement.namespace,
                            },
                        }
                    },
                    # Chart defaults are the OSS path (see VCLUSTER_CHART); the one value set
                    # is where the exported kubeconfig says the API server is. The default is
                    # localhost:8443 -- right for a laptop, useless for helm-controller. The
                    # cert's SANs cover `vcluster.<ns>`, and not `vcluster.<ns>.svc`.
                    "values": {
                        "exportKubeConfig": {
                            "server": f"https://vcluster.{placement.namespace}:443"
                        }
                    },
                },
            },
        )

    @staticmethod
    def _labels(placement: Placement) -> dict[str, str]:
        return {
            "pg.placement/isolation": placement.isolation.value,
            **({"pg.placement/vcluster": placement.vcluster} if placement.vcluster else {}),
        }
