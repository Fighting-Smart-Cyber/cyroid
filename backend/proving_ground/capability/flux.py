"""`FluxChartInstaller` -- an upstream chart becomes a `HelmRelease`, not a subprocess.

The decision, taken 2026-09-13: PROVING GROUND creates Flux custom resources and lets
`helm-controller` reconcile them. It does not shell out to the `helm` binary.

The reason is the one ADR-0003's 2026-09-10 amendment already committed to -- PG speaks the
Kubernetes API and nothing else. A `helm` subprocess would put an imperative tool holding
cluster-admin credentials inside an async service, and would have to be defended as such in an
accreditation package. A reconciler is a workload like any other, and it is declarative, so a
release that drifts is corrected rather than merely reported.

The cost, stated plainly: `source-controller` and `helm-controller` join ADR-0012's cluster
capability contract. Every profile -- AKS, on-prem, dev -- has to supply them.
"""

from __future__ import annotations

import asyncio
import time

from .kube import KubeClient
from .models import Placement
from .runtime import CapabilitySpec, InstallResult
from .specs import REPOSITORY_SCHEMES, is_repository_url

__all__ = ["FluxChartInstaller", "HELM_GROUP", "SOURCE_GROUP", "VCLUSTER_KUBECONFIG_SECRET"]

HELM_GROUP, HELM_VERSION, HELM_PLURAL = "helm.toolkit.fluxcd.io", "v2", "helmreleases"
SOURCE_GROUP, SOURCE_VERSION, SOURCE_PLURAL = "source.toolkit.fluxcd.io", "v1", "helmrepositories"

# The vcluster chart exports the vcluster's kubeconfig as `vc-<release>`; the release is named
# `vcluster` by RangeLifecycle. Its `config` key is a complete kubeconfig.
VCLUSTER_KUBECONFIG_SECRET, VCLUSTER_KUBECONFIG_KEY = "vc-vcluster", "config"


class FluxChartInstaller:
    """Installs a capability's chart by declaring it and waiting for the controller."""

    def __init__(
        self, client: KubeClient, *, timeout_seconds: int = 600, poll_seconds: float = 2
    ) -> None:
        self._kube = client
        self._timeout = timeout_seconds
        self._poll = poll_seconds

    async def install(self, spec: CapabilitySpec, placement: Placement) -> InstallResult:
        # Nothing reaches the cluster until the repository is one we would fetch from. Which
        # repositories this install permits is decided where a spec is built, against the
        # operator's allow-list (`specs.ChartRepositoryPolicy`); the shape of the URL is decided
        # here, at the sink, because it needs no configuration and so cannot be left off by an
        # install that never set the setting. An `http://` or `file://` URL declared as a
        # HelmRepository is fetched by source-controller and rendered with the permissions Flux
        # holds, so refusing before the first apply is the difference that matters.
        if not is_repository_url(spec.chart.repository):
            schemes = " or ".join(f"{s}://" for s in REPOSITORY_SCHEMES)
            raise ValueError(
                f"capability {spec.name!r} names chart repository {spec.chart.repository!r}; "
                f"only a {schemes} URL, with a host and no '.' or '..' in its path, is installed"
            )
        repo = _repo_name(spec)
        await self._kube.apply_custom_object(
            group=SOURCE_GROUP,
            version=SOURCE_VERSION,
            plural=SOURCE_PLURAL,
            namespace=placement.namespace,
            name=repo,
            body={
                "apiVersion": f"{SOURCE_GROUP}/{SOURCE_VERSION}",
                "kind": "HelmRepository",
                "metadata": {"name": repo, "namespace": placement.namespace},
                "spec": {"interval": "30m", "url": spec.chart.repository},
            },
        )
        release_spec: dict = {
            "interval": "5m",
            "chart": {
                "spec": {
                    # The chart is upstream's, used as published. There is no patch field
                    # here because there is no patching -- a capability package is a
                    # wrapper, never a fork.
                    "chart": spec.chart.name,
                    "version": spec.chart.version,
                    "sourceRef": {
                        "kind": "HelmRepository",
                        "name": repo,
                        "namespace": placement.namespace,
                    },
                }
            },
            "values": dict(spec.values),
        }
        if placement.vcluster:
            # The object stays here, reconciled by the host's Flux; it *deploys* into the
            # vcluster through the kubeconfig the vcluster chart exported (ADR-0002, 2026-09-15
            # amendment). No second Flux inside the vcluster. The target namespace inside it
            # takes the placement's name, so the two sides read the same.
            release_spec["kubeConfig"] = {
                "secretRef": {"name": VCLUSTER_KUBECONFIG_SECRET, "key": VCLUSTER_KUBECONFIG_KEY}
            }
            release_spec["targetNamespace"] = placement.namespace
            release_spec["install"] = {"createNamespace": True}
        await self._kube.apply_custom_object(
            group=HELM_GROUP,
            version=HELM_VERSION,
            plural=HELM_PLURAL,
            namespace=placement.namespace,
            name=spec.name,
            body={
                "apiVersion": f"{HELM_GROUP}/{HELM_VERSION}",
                "kind": "HelmRelease",
                "metadata": {"name": spec.name, "namespace": placement.namespace},
                "spec": release_spec,
            },
        )
        revision = await self._await_ready(spec, placement)
        return InstallResult(release=spec.name, namespace=placement.namespace, revision=revision)

    async def uninstall(self, spec: CapabilitySpec, placement: Placement) -> None:
        """Delete the release, wait for it to go, then delete its source.

        Order matters twice. A HelmRelease whose HelmRepository has already gone cannot complete
        its own uninstall. And a release deploying into a vcluster holds a finalizer until
        helm-controller has uninstalled from that vcluster -- delete the vcluster first and the
        finalizer can never clear, which strands the object and the namespace around it. So
        uninstall is not done until the object is gone.
        """
        await self._kube.delete_custom_object(
            group=HELM_GROUP,
            version=HELM_VERSION,
            plural=HELM_PLURAL,
            namespace=placement.namespace,
            name=spec.name,
        )
        await self._await_gone(spec, placement)
        await self._kube.delete_custom_object(
            group=SOURCE_GROUP,
            version=SOURCE_VERSION,
            plural=SOURCE_PLURAL,
            namespace=placement.namespace,
            name=_repo_name(spec),
        )

    async def _await_gone(self, spec: CapabilitySpec, placement: Placement) -> None:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            obj = await self._kube.get_custom_object(
                group=HELM_GROUP,
                version=HELM_VERSION,
                plural=HELM_PLURAL,
                namespace=placement.namespace,
                name=spec.name,
            )
            if obj is None:
                return
            await asyncio.sleep(self._poll)
        raise TimeoutError(
            f"release {spec.name} in {placement.namespace} did not finish uninstalling"
        )

    async def _await_ready(self, spec: CapabilitySpec, placement: Placement) -> int:
        """Wait for helm-controller to report the release Ready.

        A `Ready=False` with a reason is a failure worth surfacing immediately rather than sitting
        out the timeout: an install that will never succeed says so in seconds, and the reason is
        more useful than "timed out".
        """
        deadline = time.monotonic() + self._timeout
        last = "no status yet"
        while time.monotonic() < deadline:
            obj = await self._kube.get_custom_object(
                group=HELM_GROUP,
                version=HELM_VERSION,
                plural=HELM_PLURAL,
                namespace=placement.namespace,
                name=spec.name,
            )
            status = (obj or {}).get("status") or {}
            for condition in status.get("conditions", []):
                if condition.get("type") != "Ready":
                    continue
                if condition.get("status") == "True":
                    return int(status.get("history", [{}])[0].get("version", 1))
                last = f"{condition.get('reason')}: {condition.get('message')}"
                if condition.get("reason") in {"InstallFailed", "UpgradeFailed", "ArtifactFailed"}:
                    raise RuntimeError(f"chart install failed for {spec.name}: {last}")
            await asyncio.sleep(self._poll)
        raise TimeoutError(f"chart install for {spec.name} did not become ready: {last}")


def _repo_name(spec: CapabilitySpec) -> str:
    from .models import to_dns_label

    return to_dns_label(spec.name, "repo")
