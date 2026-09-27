"""The trainable-capability contract — and the intellectual-property boundary.

Everything that touches a capability goes through `CapabilityRuntime`. There is
exactly one implementation, `KubernetesRuntime`. See ADR-0004 for what a
capability is, and CLAUDE.md's architecture rules.

**This package is also where the licence boundary falls.** Under ADR-0011 the
engine is public and AGPL-3.0; a distribution's capability packages are its
owner's. What makes that separable in practice is that a package implements
*this* surface and nothing else: the names below are the contract, and
everything else under `proving_ground` is an internal that a package must not
reach for. The organising rule is that the engine ships the schema and the
mechanism and the distribution ships the instance data -- the contract is
engine, the package implementing it is distribution.

So the import direction matters more than it looks. This package imports
nothing from the rest of `proving_ground`, which is what stops a package built
against the contract from transitively depending on platform internals, and
what keeps the boundary a line rather than a preference.
`tests/unit/test_capability_contract_boundary.py` fails if that stops being
true; see ADR-0011's 2026-09-14 amendment.
"""

from .models import (
    Delivery,
    Isolation,
    Placement,
    PlacementRequest,
    Scope,
)
from .placement import placement_for_assignment, resolve_placement
from .runtime import (
    CapabilityRuntime,
    CapabilitySpec,
    ChartRef,
    ExecResult,
    HookResult,
    HookSpec,
    ImageRef,
    InstallResult,
    VerifyResult,
)

# The published contract. A capability package may depend on these names and on
# nothing else under `proving_ground`.
__all__ = [
    # What a capability is
    "CapabilitySpec",
    "ChartRef",
    "HookSpec",
    "ImageRef",
    "Scope",
    # What a runtime does with it
    "CapabilityRuntime",
    "ExecResult",
    "HookResult",
    "InstallResult",
    "VerifyResult",
    # Where it runs
    "Delivery",
    "Isolation",
    "Placement",
    "PlacementRequest",
    "placement_for_assignment",
    "resolve_placement",
]
