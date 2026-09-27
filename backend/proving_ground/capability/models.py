"""Value types for the capability contract.

These are deliberately plain frozen dataclasses and enums, not SQLAlchemy models. Placement is a
*decision*, not a stored provider field -- putting `vcluster_name` on `Range` would repeat the
mistake that `Range.dind_container_id` and VM's flat provider columns are being unwound from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Scope(StrEnum):
    """How much of a capability one learner gets.

    Required on every capability, with no default (CLAUDE.md architecture rule 8). `reset()` is
    scope-aware and `verify()` asserts against the learner's slice, so a missing scope is not a
    detail to infer later -- it is the thing that decides what those two operations even mean.
    """

    PER_LEARNER = "per-learner"
    PER_COHORT = "per-cohort"
    SHARED = "shared"


class Delivery(StrEnum):
    """How the content is delivered, which is what the isolation boundary follows."""

    SELF_PACED = "self-paced"
    TEAM_EXERCISE = "team-exercise"


class Isolation(StrEnum):
    """The substrate boundary a range resolves to."""

    NAMESPACE = "namespace"
    VCLUSTER = "vcluster"


_DNS1123 = re.compile(r"[^a-z0-9-]")


def to_dns_label(*parts: str, limit: int = 63) -> str:
    """Join `parts` into a DNS-1123 label.

    Kubernetes rejects anything else, and it rejects it at apply time -- which in a range deploy
    means a partial failure halfway through rather than a validation error up front.
    """
    joined = "-".join(p for p in parts if p)
    slug = _DNS1123.sub("-", joined.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise ValueError(f"cannot build a DNS-1123 label from {parts!r}")
    return slug[:limit].rstrip("-")


@dataclass(frozen=True, slots=True)
class PlacementRequest:
    """What the policy is allowed to look at.

    Note what is absent: the range's id, name and owner. Placement is decided from declared
    properties so that two ranges declaring the same thing land the same way -- "by policy, not by
    identity" is the SD-1 acceptance criterion, and the only way to hold it is to withhold identity
    from the decision.
    """

    scopes: frozenset[Scope]
    delivery: Delivery
    needs_cluster_scoped_resources: bool = False
    cohort_key: str = "default"
    learner_key: str | None = None

    def __post_init__(self) -> None:
        if not self.scopes:
            raise ValueError("a placement request declares at least one capability scope")
        if Scope.PER_LEARNER in self.scopes and not self.learner_key:
            raise ValueError("a per-learner scope requires a learner_key to slice on")


@dataclass(frozen=True, slots=True)
class Placement:
    """Where a range runs, and why.

    `reason` is part of the contract, not debug output: a placement you cannot explain is one you
    cannot defend to an assessor, and SD-1 demonstrates the decision, not just its result.
    """

    isolation: Isolation
    namespace: str
    reason: str
    vcluster: str | None = None

    def __post_init__(self) -> None:
        if self.isolation is Isolation.VCLUSTER and not self.vcluster:
            raise ValueError("vcluster isolation requires a vcluster name")
        if self.isolation is Isolation.NAMESPACE and self.vcluster:
            raise ValueError("namespace isolation must not name a vcluster")
