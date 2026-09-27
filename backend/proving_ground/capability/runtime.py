"""`CapabilityRuntime` -- the single interface every capability operation goes through.

This is the AGPL/proprietary boundary (ADR-0004, ADR-0008): a capability integration depends on
this contract and never on platform internals. It is also the seam the substrate migration turns
on -- the assessment spine is written against this interface, so the DinD path underneath can be
deleted without the spine noticing.

There is exactly one implementation, `KubernetesRuntime`. **Never write a second runtime.** A
second implementation is how "the same behaviour everywhere" quietly becomes two behaviours that
drift, and it is how the `VMProvider` abstraction died the first time.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .models import Placement, Scope

__all__ = [
    "CapabilityRuntime",
    "CapabilitySpec",
    "ChartRef",
    "ExecResult",
    "HookResult",
    "HookSpec",
    "ImageRef",
    "InstallResult",
    "VerifyResult",
]

# A digest-pinned reference. Air-gap is a constraint now, not later (ADR-0007): no runtime image
# pulls, every image referenced by digest, no public registry dependency at runtime. A tag is a
# mutable pointer, so a tag-referenced image is not the same artefact twice.
_DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ImageRef:
    """An image, pinned by digest."""

    ref: str

    def __post_init__(self) -> None:
        if not _DIGEST.match(self.ref):
            raise ValueError(
                f"image {self.ref!r} is not digest-pinned; "
                "capability images must be <repo>@sha256:<64 hex> (ADR-0007)"
            )


@dataclass(frozen=True, slots=True)
class ChartRef:
    """An upstream Helm chart, used unmodified.

    A capability package is a wrapper, never a fork (CLAUDE.md architecture rule 3). The line is
    drawn at the *artefact*: values and hooks are ours, the chart is theirs, and nothing here
    offers a way to patch it. If a capability needs the chart changed, that is a conversation with
    upstream, not a field on this dataclass.
    """

    name: str
    version: str
    repository: str

    def __post_init__(self) -> None:
        if not self.version or self.version == "latest":
            raise ValueError("chart version must be explicit and pinned -- ':latest' is banned")


@dataclass(frozen=True, slots=True)
class HookSpec:
    """A seed/reset/verify hook, run as a separate workload.

    Separate, not a chart patch and not an init container on somebody else's pod: the hook has to
    be removable without touching the thing it operates on.
    """

    image: ImageRef
    command: tuple[str, ...]
    timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError("a hook needs a command")
        if self.timeout_seconds <= 0:
            raise ValueError("a hook needs a positive timeout")


@dataclass(frozen=True, slots=True)
class WebSpec:
    """The capability's browser-facing surface: which Service, which port -- COSMOS PG-62.

    A learner reaches the application in a browser, and the range has to say where it answers.
    The URL itself is the platform's to mint (per range, authorised to the learner); the
    capability only names what to route to.
    """

    service: str
    port: int
    path: str = "/"

    def __post_init__(self) -> None:
        if not self.service:
            raise ValueError("a web surface must name a service")
        if not 0 < self.port < 65536:
            raise ValueError(f"web port {self.port} is not a TCP port")
        if not self.path.startswith("/"):
            raise ValueError(f"web path {self.path!r} must start with /")


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """A trainable capability: a package, plus seed/reset/verify, plus a scope (ADR-0004)."""

    name: str
    version: str
    chart: ChartRef
    scope: Scope
    verify: HookSpec
    images: tuple[ImageRef, ...] = ()
    values: Mapping[str, Any] = field(default_factory=dict)
    seed: HookSpec | None = None
    reset: HookSpec | None = None
    web: WebSpec | None = None

    def __post_init__(self) -> None:
        # `scope` is typed as required, but a dict-built spec can still smuggle None through.
        if not isinstance(self.scope, Scope):
            raise ValueError(
                f"capability {self.name!r} must declare a scope "
                "(per-learner | per-cohort | shared) -- there is no default"
            )


@dataclass(frozen=True, slots=True)
class InstallResult:
    release: str
    namespace: str
    revision: int


@dataclass(frozen=True, slots=True)
class HookResult:
    succeeded: bool
    logs: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """The outcome of `verify()`, and the evidence behind it.

    `evidence` is not optional decoration. The differentiator is requirements-traced, defensible
    evidence of competence -- a pass with nothing behind it is an assertion, and an assertion is
    what an assessor throws out.
    """

    passed: bool
    evidence: Mapping[str, Any]
    observed_at: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.evidence:
            raise ValueError("verify() must return evidence, not a bare boolean")


class CapabilityRuntime(ABC):
    """The one interface. `exec` is the explicit escape hatch, and is meant to look like one."""

    @abstractmethod
    async def install(self, spec: CapabilitySpec, placement: Placement) -> InstallResult:
        """Install the capability's upstream chart, unmodified, with our values."""

    @abstractmethod
    async def seed(self, spec: CapabilitySpec, placement: Placement) -> HookResult:
        """Put the capability into its starting state for a learner."""

    @abstractmethod
    async def reset(self, spec: CapabilitySpec, placement: Placement) -> HookResult:
        """Return the capability to its seeded state.

        **Scope-aware.** `per-learner` resets one learner's slice and must not disturb a peer;
        `per-cohort` resets the cohort's; `shared` resets everything and is therefore the one an
        instructor has to be asked about. An implementation that ignores `spec.scope` here is
        broken even when every test of the happy path passes -- the damage shows up as another
        learner's work vanishing mid-exercise.
        """

    @abstractmethod
    async def verify(self, spec: CapabilitySpec, placement: Placement) -> VerifyResult:
        """Assert the learner-caused state change, against the learner's own slice."""

    @abstractmethod
    async def exec(
        self, spec: CapabilitySpec, placement: Placement, command: Sequence[str]
    ) -> ExecResult:
        """Run a command inside the running capability.

        The escape hatch, named plainly so that reaching for it is a visible choice. `exec` into a
        running pod is not modification of the artefact; adding a sixth method to avoid saying
        `exec` would be.
        """

    @abstractmethod
    async def uninstall(self, spec: CapabilitySpec, placement: Placement) -> None:
        """Remove the capability. Must be safe to call when it was never installed."""
