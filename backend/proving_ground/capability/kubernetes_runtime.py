"""`KubernetesRuntime` -- the one and only implementation of `CapabilityRuntime`.

If you are here to add a second implementation, don't. See CLAUDE.md architecture rule 2 and the
`VMProvider` abstraction in `docs/plans/2026-01-12-cyroid-mvp-design.md` that was specified, never
built, and is the reason that rule is written the way it is.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

from .kube import KubeClient
from .models import Isolation, Placement, Scope
from .runtime import (
    CapabilityRuntime,
    CapabilitySpec,
    ExecResult,
    HookResult,
    HookSpec,
    InstallResult,
    VerifyResult,
)

__all__ = ["ChartInstaller", "KubernetesRuntime", "ScopeViolation"]


class ScopeViolation(RuntimeError):
    """Raised when an operation would reach outside the slice its scope entitles it to.

    This is a loud failure on purpose. The quiet version of this bug is a `per-learner` reset that
    runs against a cohort namespace and takes twenty-nine other people's work with it -- which
    surfaces as "the range is broken" an hour later, from someone who did nothing wrong.
    """


class ChartInstaller:
    """How an upstream chart becomes a running release.

    **Deliberately unimplemented.** The mechanism is an open decision -- a Flux `HelmRelease` CR
    (PROVING GROUND stays on the Kubernetes API, at the cost of requiring a reconciler in every
    deployment profile) versus shelling out to the `helm` binary (no cluster-side dependency, at
    the cost of an imperative subprocess in an async service). It is a change to ADR-0012's cluster
    capability contract either way, so it is not a detail to settle in passing.

    SD-1 does not need it: the gate is that every capability operation goes through one runtime and
    that `reset()` honours scope. Chart install is SD-3's "unmodified upstream artefact".
    """

    async def install(self, spec: CapabilitySpec, placement: Placement) -> InstallResult:
        raise NotImplementedError(
            "chart install mechanism is undecided -- Flux HelmRelease vs the helm binary. "
            "See ChartInstaller's docstring and ADR-0012."
        )

    async def uninstall(self, spec: CapabilitySpec, placement: Placement) -> None:
        raise NotImplementedError("chart install mechanism is undecided")


class KubernetesRuntime(CapabilityRuntime):
    """Capability operations, realised as Kubernetes objects."""

    def __init__(self, client: KubeClient, chart_installer: object | None = None) -> None:
        from .flux import FluxChartInstaller

        self._kube = client
        # Flux by default (decided 2026-09-13). `ChartInstaller` remains as the documented
        # not-yet type for a runtime constructed without a reconciler available.
        self._charts = chart_installer or FluxChartInstaller(client)

    # ---------------------------------------------------------------- lifecycle

    async def install(self, spec: CapabilitySpec, placement: Placement) -> InstallResult:
        await self._kube.ensure_namespace(placement.namespace, self._labels(spec, placement))
        return await self._charts.install(spec, placement)

    async def uninstall(self, spec: CapabilitySpec, placement: Placement) -> None:
        # Idempotent by construction -- a release that is not there is a no-op to delete -- so
        # a range torn down twice is not an error. No namespace check first: for a vcluster
        # placement `self._kube` is the vcluster, and a vcluster that never came up still has a
        # HelmRelease on the host whose finalizer would otherwise strand the namespace.
        await self._charts.uninstall(spec, placement)

    # ------------------------------------------------------------------- hooks

    async def seed(self, spec: CapabilitySpec, placement: Placement) -> HookResult:
        if spec.seed is None:
            return HookResult(succeeded=True, logs="no seed hook declared", duration_seconds=0.0)
        return await self._run_hook(spec, placement, spec.seed, "seed")

    async def reset(self, spec: CapabilitySpec, placement: Placement) -> HookResult:
        """Return the capability to its seeded state, within the slice `scope` entitles it to."""
        self._assert_scope_contains(spec, placement)
        if spec.reset is None:
            return HookResult(succeeded=True, logs="no reset hook declared", duration_seconds=0.0)
        await self._kube.delete_workloads(
            namespace=placement.namespace, selector=self._selector(spec)
        )
        return await self._run_hook(spec, placement, spec.reset, "reset")

    async def verify(self, spec: CapabilitySpec, placement: Placement) -> VerifyResult:
        """Assert the learner-caused state change, against the learner's own slice."""
        self._assert_scope_contains(spec, placement)
        outcome = await self._run_hook(spec, placement, spec.verify, "verify")
        evidence = _extract_evidence(outcome.logs)
        return VerifyResult(
            passed=outcome.succeeded and bool(evidence.get("passed", outcome.succeeded)),
            evidence=evidence,
            observed_at=datetime.now(UTC).isoformat(),
            detail=f"{spec.name}@{spec.version} in {placement.namespace}",
        )

    async def exec(
        self, spec: CapabilitySpec, placement: Placement, command: Sequence[str]
    ) -> ExecResult:
        if not command:
            raise ValueError("exec needs a command")
        self._assert_scope_contains(spec, placement)
        result = await self._kube.exec_in_pod(
            namespace=placement.namespace,
            selector=self._selector(spec),
            command=tuple(command),
        )
        return ExecResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)

    # ------------------------------------------------------------------ internals

    def _assert_scope_contains(self, spec: CapabilitySpec, placement: Placement) -> None:
        """Refuse an operation whose blast radius exceeds what `scope` declares.

        `per-learner` is the case that matters: its namespace must be the learner's own, which is
        the one a `PER_LEARNER` placement builds and a cohort-level placement does not. A
        `per-cohort` capability placed in a learner namespace is the inverse mistake -- it would
        reset one learner and silently leave the other twenty-nine un-reset.
        """
        if spec.scope is Scope.PER_LEARNER and placement.isolation is not Isolation.NAMESPACE:
            raise ScopeViolation(
                f"capability {spec.name!r} is per-learner but was placed on a "
                f"{placement.isolation.value}, whose blast radius is the whole range"
            )
        if spec.scope is Scope.PER_COHORT and placement.isolation is Isolation.NAMESPACE:
            # A per-cohort capability needs the cohort's boundary, not one learner's slice of it.
            if placement.vcluster is None:
                raise ScopeViolation(
                    f"capability {spec.name!r} is per-cohort but was placed in a bare namespace; "
                    "it would operate on one learner and miss the rest of the cohort"
                )

    async def _run_hook(
        self, spec: CapabilitySpec, placement: Placement, hook: HookSpec, kind: str
    ) -> HookResult:
        from .models import to_dns_label

        outcome = await self._kube.run_job(
            namespace=placement.namespace,
            name=to_dns_label(spec.name, kind),
            image=hook.image.ref,
            command=hook.command,
            timeout_seconds=hook.timeout_seconds,
        )
        return HookResult(
            succeeded=outcome.succeeded,
            logs=outcome.logs,
            duration_seconds=outcome.duration_seconds,
        )

    @staticmethod
    def _selector(spec: CapabilitySpec) -> str:
        return f"pg.capability/name={spec.name}"

    @staticmethod
    def _labels(spec: CapabilitySpec, placement: Placement) -> dict[str, str]:
        return {
            "pg.capability/name": spec.name,
            "pg.capability/version": spec.version,
            "pg.capability/scope": spec.scope.value,
            "pg.placement/isolation": placement.isolation.value,
        }


def _extract_evidence(logs: str) -> dict:
    """Pull the evidence object a verify hook printed.

    The contract with a hook author is one line of JSON on stdout. Hooks print other things too --
    progress, warnings, whatever the tool they wrap emits -- so the *last* JSON object wins rather
    than the whole of stdout having to be clean. A hook that retries prints its failure first and
    its success last, and the last word is the one that counts.

    A hook that prints nothing parseable still produces evidence: what it actually said. A silent
    pass would be exactly the bare boolean `VerifyResult` exists to refuse.
    """
    text = logs or ""

    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{"):
            parsed = _loads_object(stripped)
            if parsed is not None:
                return parsed

    # Fall back to a pretty-printed object spanning several lines. Scanned from the last opening
    # brace backwards so a trailing object still beats an earlier one.
    opens = [i for i, ch in enumerate(text) if ch == "{"]
    for start in reversed(opens):
        parsed = _loads_object(text[start:].rstrip())
        if parsed is not None:
            return parsed

    return {"raw_output": text.strip() or "<no output>"}


def _loads_object(candidate: str) -> dict | None:
    """`json.loads` narrowed to objects -- an array or a bare scalar is not evidence."""
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
