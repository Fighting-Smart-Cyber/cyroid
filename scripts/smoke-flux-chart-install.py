#!/usr/bin/env python3
"""Install a real upstream chart through `FluxChartInstaller` against a real cluster.

Companion to `smoke-capability-runtime.py`, kept separate because this one needs egress to a chart
repository — on an air-gapped host it is expected to fail, and that failure is information rather
than a regression.

Requires `source-controller` and `helm-controller` in the cluster (ADR-0012, 2026-09-13 amendment):

    flux install --components=source-controller,helm-controller

Then:

    .venv/bin/python scripts/smoke-flux-chart-install.py

podinfo is used because it is the chart Flux's own documentation installs: small, public, and
genuinely upstream — which is the property under test. Nothing here patches it.
"""

import asyncio
import sys

from proving_ground.capability.flux import HELM_GROUP, HELM_VERSION, FluxChartInstaller
from proving_ground.capability.kubernetes_client import KubernetesApiClient
from proving_ground.capability.models import Isolation, Placement, Scope
from proving_ground.capability.runtime import CapabilitySpec, ChartRef, HookSpec, ImageRef

BUSYBOX = (
    "docker.io/library/busybox@sha256:"
    "73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
)

SPEC = CapabilitySpec(
    name="podinfo",
    version="1.0.0",
    chart=ChartRef(
        name="podinfo", version="6.7.1", repository="https://stefanprodan.github.io/podinfo"
    ),
    scope=Scope.SHARED,
    verify=HookSpec(image=ImageRef(BUSYBOX), command=("/bin/true",)),
    values={"replicaCount": 1},
)
PLACEMENT = Placement(
    isolation=Isolation.NAMESPACE, namespace="pg-flux-smoke", reason="chart install smoke"
)


async def main() -> int:
    kube = await KubernetesApiClient.from_kubeconfig()
    installer = FluxChartInstaller(kube, timeout_seconds=300)
    ok = True
    try:
        await kube.ensure_namespace(PLACEMENT.namespace, {})
        print(f"NAMESPACE   {PLACEMENT.namespace}")
        print(f"CHART       {SPEC.chart.name}@{SPEC.chart.version} from {SPEC.chart.repository}")

        result = await installer.install(SPEC, PLACEMENT)
        print(f"INSTALL     release={result.release} revision={result.revision}")

        release = await kube.get_custom_object(
            group=HELM_GROUP,
            version=HELM_VERSION,
            plural="helmreleases",
            namespace=PLACEMENT.namespace,
            name=SPEC.name,
        )
        ready = next(
            c for c in (release or {}).get("status", {}).get("conditions", []) if c["type"] == "Ready"
        )
        print(f"HELMRELEASE Ready={ready['status']} reason={ready.get('reason')}")
        ok &= ready["status"] == "True"

        await installer.uninstall(SPEC, PLACEMENT)
        print("UNINSTALL   HelmRelease and HelmRepository deleted")
    except Exception as exc:  # noqa: BLE001 - the smoke reports, it does not handle
        print(f"ERROR       {type(exc).__name__}: {exc}")
        ok = False
    finally:
        await kube.delete_namespace(PLACEMENT.namespace)
        print("CLEANUP     namespace deleted")
        await kube.close()

    print("\n" + ("FLUX SMOKE PASSED" if ok else "FLUX SMOKE FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
