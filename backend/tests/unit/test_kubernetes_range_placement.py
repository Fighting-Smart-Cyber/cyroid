"""Deriving a range's placement from the columns it already has.

No new columns: `training_event_id` is the cohort, `assigned_to_user_id` is the learner, and its
presence is what distinguishes self-paced from a team exercise.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from proving_ground.capability.models import Isolation, Scope
from proving_ground.capability.runtime import CapabilitySpec, ChartRef, HookSpec, ImageRef
from proving_ground.capability import placement_for_assignment

DIGEST = "registry.example/logc2@sha256:" + "e" * 64


def cap(scope=Scope.PER_LEARNER, **values):
    return CapabilitySpec(
        name="logc2",
        version="1.0.0",
        chart=ChartRef(name="logc2", version="1.4.2", repository="https://charts.example"),
        scope=scope,
        verify=HookSpec(image=ImageRef(DIGEST), command=("/v",)),
        values=values,
    )


def place(range_obj, capabilities):
    return placement_for_assignment(
        range_id=range_obj.id,
        training_event_id=range_obj.training_event_id,
        assigned_to_user_id=range_obj.assigned_to_user_id,
        capabilities=capabilities,
    )


def rng(event=None, learner=None):
    return SimpleNamespace(id=uuid4(), training_event_id=event, assigned_to_user_id=learner)


class TestDerivation:
    def test_an_assigned_learner_means_self_paced_and_a_namespace(self):
        p = place(rng(event=uuid4(), learner=uuid4()), [cap()])
        assert p.isolation is Isolation.NAMESPACE

    def test_an_event_with_nobody_assigned_is_a_team_exercise(self):
        p = place(rng(event=uuid4()), [cap(Scope.SHARED)])
        assert p.isolation is Isolation.VCLUSTER

    def test_two_learners_in_one_cohort_get_different_namespaces(self):
        event = uuid4()
        a = place(rng(event=event, learner=uuid4()), [cap()])
        b = place(rng(event=event, learner=uuid4()), [cap()])
        assert a.namespace != b.namespace

    def test_the_same_cohort_yields_the_same_vcluster(self):
        event = uuid4()
        a = place(rng(event=event, learner=uuid4()), [cap(Scope.PER_COHORT)])
        b = place(rng(event=event, learner=uuid4()), [cap(Scope.PER_COHORT)])
        assert a.vcluster == b.vcluster or (a.vcluster is None and b.vcluster is None)

    def test_a_range_with_no_capabilities_still_places(self):
        assert place(rng(), []).namespace

    def test_two_standalone_ranges_never_share_a_namespace(self):
        # An instructor deploying two instances of a blueprint, no event, no learner. Before
        # this, both resolved to cohort "default" -> namespace pg-default, and the second's
        # `web` VM replaced the first's. The range's own id is the cohort when there is no event.
        a, b = place(rng(), [cap(Scope.SHARED)]), place(rng(), [cap(Scope.SHARED)])
        assert a.namespace != b.namespace
        assert a.namespace.startswith("pg-range-")

    def test_a_standalone_range_is_a_namespace_not_a_team_exercise(self):
        # Nobody assigned and no event is one person's sandbox, not a team sharing a blast
        # radius. A vcluster per instructor sandbox is the cost model PG-41 exists to avoid.
        assert place(rng(), [cap(Scope.SHARED)]).isolation is Isolation.NAMESPACE

    def test_a_learner_without_an_event_gets_a_namespace_keyed_on_the_range(self):
        p = place(rng(learner=uuid4()), [cap()])
        assert p.isolation is Isolation.NAMESPACE
        assert p.namespace.startswith("pg-range-")

    def test_a_cluster_scoped_capability_forces_a_vcluster(self):
        p = place(rng(learner=uuid4()), [cap(clusterScoped=True)])
        assert p.isolation is Isolation.VCLUSTER


class TestALearnerAssignmentIsAnIsolationBoundary:
    """Whatever the capabilities declare. This is the rule that was missing.

    Every blueprint shipped today declares workloads and shared-scope capabilities, so the
    per-learner scope that used to be the only thing keying a namespace on a learner was never
    present in practice: a thirty-learner cohort resolved to one namespace and each deploy
    applied over the last.
    """

    def test_two_learners_sharing_a_cohort_and_no_per_learner_scope_are_still_separated(self):
        event = uuid4()
        a = place(rng(event=event, learner=uuid4()), [cap(Scope.SHARED)])
        b = place(rng(event=event, learner=uuid4()), [cap(Scope.SHARED)])
        assert a.namespace != b.namespace

    @pytest.mark.parametrize("scope", [Scope.SHARED, Scope.PER_COHORT, Scope.PER_LEARNER])
    @pytest.mark.parametrize("cluster_scoped", [False, True])
    def test_no_declaration_reaches_a_branch_that_shares_a_learner_namespace(
        self, scope, cluster_scoped
    ):
        # The fix has to hold in every branch that can see a learner, not only the one the
        # collapse was found in: a blueprint chooses its scopes and whether anything it installs
        # is cluster-scoped, and neither choice is the learner's to make.
        event = uuid4()
        caps = [cap(scope, clusterScoped=True)] if cluster_scoped else [cap(scope)]
        a = place(rng(event=event, learner=uuid4()), caps)
        b = place(rng(event=event, learner=uuid4()), caps)
        assert a.namespace != b.namespace

    def test_the_namespace_is_keyed_on_the_learner_and_not_merely_unique(self):
        # Two namespaces differing is not the property; being the learner's is. Keyed on the
        # range they would differ too, and every test above would still pass while the boundary
        # was the wrong one -- so this one reads the key itself.
        event, learner = uuid4(), uuid4()
        p = place(rng(event=event, learner=learner), [cap(Scope.SHARED)])
        assert p.namespace.startswith(f"pg-{event}")
        assert str(learner)[:8] in p.namespace

    def test_the_same_learner_and_cohort_always_resolve_to_the_same_namespace(self):
        # Redeploying a range has to land where its machines and volumes already are.
        event, learner = uuid4(), uuid4()
        a = place(rng(event=event, learner=learner), [cap(Scope.SHARED)])
        b = place(rng(event=event, learner=learner), [cap(Scope.SHARED)])
        assert a.namespace == b.namespace

    def test_a_cohort_with_no_capabilities_at_all_still_separates_its_learners(self):
        # A workloads-only blueprint declares no capability, so `scopes` falls back to SHARED.
        event = uuid4()
        a = place(rng(event=event, learner=uuid4()), [])
        b = place(rng(event=event, learner=uuid4()), [])
        assert a.namespace != b.namespace

    def test_a_cluster_scoped_cohort_still_separates_its_learners(self):
        # A vcluster shared across the cohort, a namespace each inside it.
        event = uuid4()
        a = place(rng(event=event, learner=uuid4()), [cap(Scope.SHARED, clusterScoped=True)])
        b = place(rng(event=event, learner=uuid4()), [cap(Scope.SHARED, clusterScoped=True)])
        assert a.isolation is Isolation.VCLUSTER and a.vcluster == b.vcluster
        assert a.namespace != b.namespace

    def test_a_team_exercise_still_shares_one_namespace(self):
        # The one case where sharing is the point: nobody is assigned, the team is the unit.
        event = uuid4()
        a = place(rng(event=event), [cap(Scope.SHARED)])
        b = place(rng(event=event), [cap(Scope.SHARED)])
        assert a.namespace == b.namespace

    def test_an_assigned_range_is_never_derived_as_a_team_exercise(self):
        # The team-exercise branch is the one that keeps the cohort's namespace with a learner
        # present, and it is safe only because an assignment never reaches it. Asserted rather
        # than assumed: the delivery mode is derived here, a line above the branch that trusts it.
        p = place(rng(event=uuid4(), learner=uuid4()), [cap(Scope.SHARED)])
        assert p.isolation is Isolation.NAMESPACE
        assert "team exercise" not in p.reason

    def test_the_reason_says_what_separated_them(self):
        # The reason is shown in the range's Activity feed, so it has to be true.
        p = place(rng(event=uuid4(), learner=uuid4()), [cap(Scope.SHARED)])
        assert "assigned" in p.reason

    def test_a_namespace_placement_never_promises_a_vcluster(self):
        # It used to: the per-learner reason read "inside the cohort's vcluster, so the control
        # plane is shared across the cohort", while the placement carried no vcluster and the
        # lifecycle built none. The Activity feed is the record of where a range went.
        for caps in ([cap(Scope.PER_LEARNER)], [cap(Scope.SHARED)], []):
            p = place(rng(event=uuid4(), learner=uuid4()), caps)
            assert p.vcluster is None
            assert "vcluster" not in p.reason

    def test_a_cluster_scoped_learner_range_says_where_the_learner_went(self):
        # A vcluster and a namespace at once is the branch a reader is most likely to get wrong,
        # so the reason names both halves -- and names them the way the lifecycle builds them.
        # It read "a vcluster for the cohort, and a namespace inside it": the chart is installed
        # into the range's namespace rather than around it, and once that namespace is keyed on
        # the learner there is a control plane per learner and none the cohort shares. Both
        # halves were wrong, in the branch a learner-keyed namespace made wrong.
        p = place(rng(event=uuid4(), learner=uuid4()), [cap(Scope.SHARED, clusterScoped=True)])
        assert "cluster-scoped" in p.reason
        assert "learner" in p.reason
        assert "for the cohort" not in p.reason
        assert "inside it" not in p.reason

    def test_a_cluster_scoped_team_exercise_keeps_the_cohort_namespace_and_says_so(self):
        # The other arm of the same branch, which nothing covered: nobody is assigned, so there
        # is no learner to key on and the cohort's namespace is the right answer. The reason has
        # to say that rather than reuse the learner's wording.
        event = uuid4()
        a = place(rng(event=event), [cap(Scope.SHARED, clusterScoped=True)])
        b = place(rng(event=event), [cap(Scope.SHARED, clusterScoped=True)])
        assert a.namespace == b.namespace == f"pg-{event}"
        assert "cohort" in a.reason and "learner" not in a.reason


class TestRefusals:
    def test_per_learner_with_nobody_assigned_is_refused(self):
        # Seeding one shared slice and calling it thirty learners' work is the failure this
        # prevents, and it is silent if it is allowed through.
        with pytest.raises(ValueError, match="no assigned learner"):
            place(rng(event=uuid4()), [cap(Scope.PER_LEARNER)])

    def test_a_standalone_range_with_a_per_learner_capability_is_refused_too(self):
        # No event either, so the refusal comes from the general branch rather than the one that
        # explains a team exercise. Both paths have to refuse -- an instructor's own sandbox is
        # no more a learner's slice than a cohort range is -- and this one must not answer with
        # the team exercise's advice, because there is no event here to have started either way.
        with pytest.raises(ValueError) as refused:
            place(rng(), [cap(Scope.PER_LEARNER)])
        assert "team exercise" not in str(refused.value)
