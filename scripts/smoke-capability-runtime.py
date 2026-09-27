#!/usr/bin/env python3
"""Exercise `KubernetesRuntime` end to end against a real cluster.

Not a unit test and deliberately not in `tests/`: it needs a cluster, and CI has none. The unit
suite proves the runtime's logic against a fake; this proves the client actually speaks to
Kubernetes, which is the half a fake can never tell you about.

    ssh pg-devtest
    export KUBECONFIG=$HOME/.kube/config
    python3 -m venv .venv && .venv/bin/pip install kubernetes-asyncio==36.1.0
    .venv/bin/python scripts/smoke-capability-runtime.py

It creates one namespace, runs the three hooks in it, asserts that a deliberate scope violation is
refused, and deletes the namespace on the way out -- including when a step fails.
"""

import asyncio
import sys

from proving_ground.capability import (
    Delivery,
    PlacementRequest,
    Scope,
    resolve_placement,
)
from proving_ground.capability.kubernetes_client import KubernetesApiClient
from proving_ground.capability.kubernetes_runtime import (
    KubernetesRuntime,
    ScopeViolation,
)
from proving_ground.capability.models import Isolation, Placement
from proving_ground.capability.runtime import (
    CapabilitySpec,
    ChartRef,
    HookSpec,
    ImageRef,
)

# Digest-pinned because the contract requires it -- a tag would be rejected by ImageRef, which is
# the point of putting that rule in the type rather than in a review checklist.
BUSYBOX = "docker.io/library/busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"


def hook(script: str) -> HookSpec:
    return HookSpec(
        image=ImageRef(BUSYBOX), command=("/bin/sh", "-c", script), timeout_seconds=120
    )


SPEC = CapabilitySpec(
    name="smoke",
    version="0.1.0",
    chart=ChartRef(name="smoke", version="0.0.1", repository="https://example.invalid"),
    scope=Scope.PER_LEARNER,
    seed=hook("echo seeding; echo '{\"seeded\": true}'"),
    reset=hook("echo resetting"),
    verify=hook('echo checking convoy; echo \'{"passed": true, "convoy_id": "C-17"}\''),
)


async def main() -> int:
    placement = resolve_placement(
        PlacementRequest(
            scopes=frozenset({Scope.PER_LEARNER}),
            delivery=Delivery.SELF_PACED,
            cohort_key="alpha",
            learner_key="lcpl-jones",
        )
    )
    print(f"PLACEMENT  {placement.isolation.value} ns={placement.namespace}")
    print(f"  reason:  {placement.reason}")

    kube = await KubernetesApiClient.from_kubeconfig()
    runtime = KubernetesRuntime(kube)
    ok = True
    try:
        await kube.ensure_namespace(
            placement.namespace, {"pg.capability/name": SPEC.name}
        )
        print(
            f"NAMESPACE  created, exists={await kube.namespace_exists(placement.namespace)}"
        )

        seeded = await runtime.seed(SPEC, placement)
        print(
            f"SEED       succeeded={seeded.succeeded} in {seeded.duration_seconds:.1f}s"
        )
        ok &= seeded.succeeded

        verified = await runtime.verify(SPEC, placement)
        print(f"VERIFY     passed={verified.passed} evidence={dict(verified.evidence)}")
        ok &= verified.passed and verified.evidence.get("convoy_id") == "C-17"

        reset = await runtime.reset(SPEC, placement)
        print(f"RESET      succeeded={reset.succeeded}")
        ok &= reset.succeeded

        # Scope enforcement has to hold against a real cluster, not only against the fake.
        violation = Placement(
            isolation=Isolation.VCLUSTER,
            namespace=placement.namespace,
            vcluster="pg-cohort-alpha",
            reason="deliberate violation",
        )
        try:
            await runtime.reset(SPEC, violation)
            print("SCOPE      FAIL - a per-learner reset on a vcluster was allowed")
            ok = False
        except ScopeViolation as exc:
            print(f"SCOPE      refused as designed: {str(exc)[:70]}...")
    finally:
        await kube.delete_namespace(placement.namespace)
        print("CLEANUP    namespace deleted")
        await kube.close()

    print("\n" + ("SMOKE PASSED" if ok else "SMOKE FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
