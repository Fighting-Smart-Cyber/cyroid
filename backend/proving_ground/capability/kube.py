"""The narrow slice of Kubernetes that `KubernetesRuntime` actually needs.

A port, not a wrapper around the whole API. Two reasons it is this small: the runtime's logic --
scope-aware reset, evidence extraction, placement realisation -- is testable against a fake without
a cluster, and the surface that has to be re-verified when the client library moves is six methods
rather than the whole of `kubernetes_asyncio`.

This is emphatically not a second runtime (CLAUDE.md architecture rule 2). It has no policy in it;
every decision lives in `KubernetesRuntime` and `placement.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["JobOutcome", "KubeClient", "PodExec", "VncStream"]


@dataclass(frozen=True, slots=True)
class JobOutcome:
    succeeded: bool
    logs: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class PodExec:
    exit_code: int
    stdout: str
    stderr: str


@runtime_checkable
class VncStream(Protocol):
    """A VM's VNC framebuffer stream, as bytes in both directions.

    What KubeVirt's `virtualmachineinstances/{name}/vnc` subresource is: a websocket carrying
    the RFB protocol straight from the launcher pod's QEMU. Nothing here understands RFB; the
    browser's noVNC does, and the API's job is to pipe.
    """

    async def send(self, data: bytes) -> None: ...

    async def receive(self) -> bytes | None:
        """The next frame, or None when the VM side closed."""
        ...


@runtime_checkable
class KubeClient(Protocol):
    """Everything the runtime is allowed to do to a cluster."""

    async def ensure_namespace(self, name: str, labels: Mapping[str, str]) -> None:
        """Make the namespace exist, and make it usable by whoever is calling.

        On a real cluster the second half is a RoleBinding: every other method here operates
        inside a namespace, and an implementation may hold those permissions per namespace
        rather than cluster-wide. That is `KubernetesApiClient`'s business and not part of this
        port -- a fake cluster has no RBAC to satisfy.
        """
        ...

    async def delete_namespace(self, name: str) -> None: ...

    async def namespace_exists(self, name: str) -> bool: ...

    async def run_job(
        self,
        *,
        namespace: str,
        name: str,
        image: str,
        command: Sequence[str],
        timeout_seconds: int,
    ) -> JobOutcome: ...

    async def exec_in_pod(
        self, *, namespace: str, selector: str, command: Sequence[str]
    ) -> PodExec: ...

    async def delete_workloads(self, *, namespace: str, selector: str) -> None: ...

    # Custom resources. Present because the chart installer creates Flux objects rather than
    # shelling out to helm -- PROVING GROUND speaks the Kubernetes API and nothing else
    # (ADR-0003, 2026-09-10 amendment).

    async def apply_custom_object(
        self,
        *,
        group: str,
        version: str,
        plural: str,
        namespace: str,
        name: str,
        body: Mapping[str, object],
    ) -> None: ...

    async def get_custom_object(
        self, *, group: str, version: str, plural: str, namespace: str, name: str
    ) -> dict | None: ...

    async def delete_custom_object(
        self, *, group: str, version: str, plural: str, namespace: str, name: str
    ) -> None: ...

    # Lifecycle. `stop` is a scale to zero rather than a delete, because a stopped range must come
    # back with the learner's work intact -- that is the whole difference between stop and destroy.

    async def scale_workloads(self, *, namespace: str, replicas: int) -> int: ...

    async def set_virtual_machines_running(self, *, namespace: str, running: bool) -> int: ...

    async def apply_network_policy(self, *, namespace: str, body: Mapping[str, object]) -> None: ...

    async def apply_ingress(self, *, namespace: str, body: Mapping[str, object]) -> None: ...

    async def list_ingresses_for_class(self, ingress_class: str) -> list[Mapping[str, object]]:
        """Every Ingress on the cluster with this ingressClassName.

        How pg-gateway learns what is published: a range's applications are declared as ordinary
        Ingress objects naming a class no controller serves, so they are garbage-collected with
        the range's namespace and visible to `kubectl get ing -A` like anything else.
        """
        ...

    async def cluster_rbac_matching(self, needle: str) -> list[str]: ...

    # Diagnostics. A VM that never leaves Provisioning says why in its pod's events, not in its
    # own status; a timeout that cannot quote them sends someone to kubectl for the reason.

    async def recent_warnings(self, *, namespace: str, limit: int = 10) -> list[str]: ...

    # A vcluster exports its kubeconfig as a Secret; that is how the runtime reaches inside it.

    async def get_secret(self, *, namespace: str, name: str) -> dict[str, str] | None: ...

    async def list_cluster_custom_objects(
        self, *, group: str, version: str, plural: str, label_selector: str
    ) -> list[dict]: ...

    # Console. A VM's VNC comes off the launcher pod through the API server, not off a service
    # the workload exposes -- which is what makes it reachable without a range network route.

    def open_vnc(self, *, namespace: str, name: str) -> AbstractAsyncContextManager[VncStream]: ...

    async def list_persistent_volumes_matching(self, needle: str) -> list[str]: ...
