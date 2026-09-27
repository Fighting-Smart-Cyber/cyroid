"""Range placement policy -- COSMOS PG #41.

A range resolves to a vcluster or a namespace **by policy, not by identity**. That is two
decisions, and answering them as one is what collapsed a cohort. Which boundary:

    needs cluster-scoped resources ->  vcluster, always. A namespace cannot hold a CRD.
    team exercise                  ->  vcluster per range. The team shares one environment and
                                       one blast radius; that boundary is the range itself.
    self-paced                     ->  a namespace, and no vcluster with it.

The third case is the one that shapes the architecture: a cohort of thirty self-paced learners is
thirty namespaces, not thirty vclusters. Per-learner vclusters would multiply the control planes
by the class size, which is the cost model that makes classroom-scale delivery unaffordable. So a
namespace placement names no vcluster at all -- `Placement` refuses that combination outright --
and a reason shown for one must not promise a control plane that was never built.

The first case is where that cost comes back, and the module has to say so rather than let the
paragraph above be read as a guarantee: `RangeLifecycle` installs the vcluster chart into the
placement's namespace, so keying that namespace on the learner puts a control plane in each of
them, and a cluster-scoped blueprint costs the class size after all. `Placement.vcluster` names
one per cohort, which is a label on those control planes rather than the address of a shared
one. Closing that means realising a cohort vcluster the learner namespaces install into -- not
keying the namespace back on the cohort, which reopens the collapse described below.

And which namespace, cutting across all three: **a learner assignment is itself an isolation
boundary.** The namespace is keyed on the learner whenever the request carries one, whatever the
capabilities declare; only a range nobody is assigned to falls back to the cohort's own. A team
exercise is the one shape that would keep the cohort's namespace with a learner present, and it
is never derived with one.

The policy used to answer the second question from the declared scopes alone, so a cohort whose
blueprint named only workloads or shared-scope capabilities -- which is every blueprint shipped
today -- put all thirty learners in `pg-<event>`. Each deploy applied over the last: same
namespace, same machine names, same volumes, and learner 2 took learner 1's machine with nothing
reported anywhere.
"""

from __future__ import annotations

from .models import Delivery, Isolation, Placement, PlacementRequest, Scope

__all__ = ["placement_for_assignment", "resolve_placement"]


def resolve_placement(request: PlacementRequest) -> Placement:
    """Decide where a range runs.

    Deterministic and total: the same declared properties always give the same answer, and every
    branch names its reason.
    """
    cohort_vcluster = _vcluster_name(request.cohort_key)
    # Keyed on the learner whenever there is one -- see the module docstring. `_namespace_name`
    # falls back to the cohort's own name when there is not, so this is the cohort namespace for
    # a range nobody is assigned to.
    namespace = _namespace_name(request)

    if request.needs_cluster_scoped_resources:
        # `RangeLifecycle.create` installs the vcluster chart *into* this namespace, so the
        # reason says "in" and not "inside it", and it does not offer the cohort a control plane
        # it shares: with the namespace keyed on the learner there is one per learner. Promising
        # a shared one here would be the lie the per-learner branch below used to tell.
        where = (
            "the namespace of the learner this range is assigned to"
            if request.learner_key
            else "the cohort's own namespace"
        )
        return Placement(
            isolation=Isolation.VCLUSTER,
            namespace=namespace,
            vcluster=cohort_vcluster,
            reason=(
                "a capability declares cluster-scoped resources, which a namespace cannot hold: "
                f"the range gets a control plane of its own, in {where}"
            ),
        )

    if request.delivery is Delivery.TEAM_EXERCISE:
        # The only branch that stays on the cohort's namespace with a learner present, and it is
        # deliberate: a team exercise is several people in one environment, so sharing it is the
        # point. `placement_for_assignment` reaches this branch with no learner, because the
        # cohort's range is assigned to nobody; `resolve_placement` is also callable on its own
        # and a caller may hand it a learner, which changes nothing about where the range goes.
        return Placement(
            isolation=Isolation.VCLUSTER,
            namespace=_namespace_name(request, root=True),
            vcluster=cohort_vcluster,
            reason="a team exercise shares one environment, so the range is the isolation boundary",
        )

    # A per-cohort capability is meant to exist once for the whole cohort, and keying the
    # namespace on the learner gives it one instance per learner instead. That is wasteful and
    # it is not what the scope means -- but the alternative this replaces was sharing the whole
    # namespace, which shared the learners' machines and volumes with it. Sharing one capability
    # properly needs a second namespace per cohort that the learner namespaces install into;
    # until that exists, the cheaper wrong answer is the safe one. It is not a silent one either:
    # the runtime refuses `reset` and `verify` for a per-cohort capability in a bare namespace
    # rather than operating on one learner's slice and reporting it as the cohort's.
    if Scope.PER_LEARNER in request.scopes:
        return Placement(
            isolation=Isolation.NAMESPACE,
            namespace=namespace,
            reason=(
                "self-paced delivery and a per-learner capability: a namespace of this learner's "
                "own, which is the slice reset and verify are entitled to act on"
            ),
        )

    if request.learner_key:
        return Placement(
            isolation=Isolation.NAMESPACE,
            namespace=namespace,
            reason=(
                "self-paced delivery: a namespace for the learner this range is assigned to. "
                "Nothing declared here asks to be sliced per learner, but the assignment does -- "
                "learners sharing one namespace deploy over each other's machines"
            ),
        )

    return Placement(
        isolation=Isolation.NAMESPACE,
        namespace=namespace,
        reason=(
            "self-paced delivery with nobody assigned: there is no learner to key a namespace "
            "on, so it is keyed on the cohort -- which, for a range that belongs to no training "
            "event, is the range itself"
        ),
    )


def _vcluster_name(cohort_key: str) -> str:
    from .models import to_dns_label

    return to_dns_label("pg", "cohort", cohort_key)


def _namespace_name(request: PlacementRequest, *, root: bool = False) -> str:
    from .models import to_dns_label

    if root or not request.learner_key:
        return to_dns_label("pg", request.cohort_key)
    return to_dns_label("pg", request.cohort_key, request.learner_key)


def placement_for_assignment(
    *,
    range_id,
    training_event_id: str | None,
    assigned_to_user_id: str | None,
    capabilities,
) -> Placement:
    """Derive a range's placement from what the range already records.

    Takes plain values, not a `Range` row, so the capability layer never imports the ORM -- and by
    extension never drags in `services/__init__`, which imports the Docker SDK and would pull Era A
    into every module that merely wants to know where a range goes (architecture rule 1).

    The mapping uses existing columns rather than new ones (rule 4):

        training_event_id   -> the cohort
        assigned_to_user_id -> the learner, and its presence means self-paced

    A range attached to an event but assigned to nobody is the team exercise: several people in one
    environment. That shape is not an accident of the data -- it is what `POST /training-events/
    {id}/start?delivery=team-exercise` creates, one range for the cohort with every student linked
    to it through their participant row rather than through an assignment. A range assigned to one
    learner is self-paced whether or not it hangs off an event. A range with neither is standalone
    -- an instructor's own sandbox -- and gets a namespace of its own, keyed on the range: two such
    ranges resolving to one shared "default" cohort is how the second one's `web` VM replaced the
    first's.

    The pair (event, learner) is the whole key for an assigned range, and it has to be: a redeploy
    must land in the namespace the learner's machines and volumes are already in, which keying on
    the range would prevent. The corollary is that two live ranges assigned to one learner under
    one event resolve to one namespace and deploy over each other. Placement cannot tell them
    apart without taking the range's identity, which is the thing it is not allowed to read -- so
    whatever hands out assignments owes it that a learner has one range per event at a time.
    """
    cohort_key = str(training_event_id) if training_event_id else f"range-{range_id}"
    learner_key = str(assigned_to_user_id) if assigned_to_user_id else None
    delivery = (
        Delivery.TEAM_EXERCISE if training_event_id and not learner_key else Delivery.SELF_PACED
    )

    scopes = frozenset(c.scope for c in capabilities) or frozenset({Scope.SHARED})
    if Scope.PER_LEARNER in scopes and not learner_key:
        # Declared per-learner but nobody is assigned. Failing here beats seeding one shared slice
        # and calling it thirty learners' work -- that failure is silent and arrives late.
        #
        # For a team exercise this is the blueprint and the delivery mode contradicting each
        # other rather than a missing assignment, so the message says which, and says it in the
        # instructor's terms: they chose the mode, and they can choose the other one.
        if delivery is Delivery.TEAM_EXERCISE:
            raise ValueError(
                "this range is a team exercise -- one environment the cohort shares, so it has "
                "no assigned learner -- and a per-learner capability cannot be sliced inside "
                "one. Start the event as self-paced, or use a blueprint whose capabilities are "
                "per-cohort or shared"
            )
        raise ValueError(
            "this blueprint declares a per-learner capability, which needs a learner to slice "
            "for, and this range is assigned to nobody. Deploy it through a training event: "
            "starting one creates a range per participant, each assigned to that learner. "
            "Deploying it directly produces a range with no learner, which is what this is"
        )

    return resolve_placement(
        PlacementRequest(
            scopes=scopes,
            delivery=delivery,
            needs_cluster_scoped_resources=any(
                c.values.get("clusterScoped") is True for c in capabilities
            ),
            cohort_key=cohort_key,
            learner_key=learner_key,
        )
    )
