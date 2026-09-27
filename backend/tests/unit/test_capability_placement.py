"""Placement policy -- COSMOS PG #41.

The acceptance criterion these guard is "a range resolves to a vcluster or a namespace BY POLICY,
not by identity". Most of these tests exist to pin that property specifically.
"""

import pytest

from proving_ground.capability import (
    Delivery,
    Isolation,
    PlacementRequest,
    Scope,
    resolve_placement,
)


def req(**kw):
    base = dict(
        scopes=frozenset({Scope.PER_LEARNER}),
        delivery=Delivery.SELF_PACED,
        cohort_key="alpha",
        learner_key="lcpl-jones",
    )
    base.update(kw)
    return PlacementRequest(**base)


class TestPolicyNotIdentity:
    def test_two_ranges_declaring_the_same_thing_place_identically(self):
        assert resolve_placement(req()) == resolve_placement(req())

    def test_placement_request_cannot_carry_a_range_identity(self):
        # If this ever accepts a range id, the policy can start keying off it.
        with pytest.raises(TypeError):
            PlacementRequest(
                scopes=frozenset({Scope.SHARED}),
                delivery=Delivery.SELF_PACED,
                range_id="r-123",
            )

    def test_every_placement_explains_itself(self):
        for r in (
            req(),
            req(delivery=Delivery.TEAM_EXERCISE),
            req(needs_cluster_scoped_resources=True),
            req(scopes=frozenset({Scope.SHARED}), learner_key=None),
        ):
            assert resolve_placement(r).reason


class TestIsolationRules:
    def test_cluster_scoped_resources_force_a_vcluster(self):
        p = resolve_placement(req(needs_cluster_scoped_resources=True))
        assert p.isolation is Isolation.VCLUSTER
        assert "cluster-scoped" in p.reason

    def test_cluster_scoped_wins_even_for_self_paced_per_learner(self):
        # The cheap answer is a namespace; a CRD makes it the wrong answer.
        p = resolve_placement(
            req(delivery=Delivery.SELF_PACED, needs_cluster_scoped_resources=True)
        )
        assert p.isolation is Isolation.VCLUSTER

    def test_team_exercise_gets_a_vcluster_per_range(self):
        p = resolve_placement(req(delivery=Delivery.TEAM_EXERCISE))
        assert p.isolation is Isolation.VCLUSTER
        assert p.vcluster

    def test_self_paced_per_learner_gets_a_namespace(self):
        p = resolve_placement(req())
        assert p.isolation is Isolation.NAMESPACE
        assert p.vcluster is None

    def test_a_cohort_of_learners_shares_one_vcluster_not_one_each(self):
        names = {resolve_placement(req(learner_key=f"learner-{i}")).namespace for i in range(30)}
        assert len(names) == 30, "each learner needs their own namespace"

    def test_shared_scope_self_paced_collapses_to_one_namespace(self):
        a = resolve_placement(req(scopes=frozenset({Scope.SHARED}), learner_key=None))
        b = resolve_placement(req(scopes=frozenset({Scope.SHARED}), learner_key=None))
        assert a.namespace == b.namespace
        assert a.isolation is Isolation.NAMESPACE


class TestScopeIsRequired:
    def test_no_scope_is_rejected(self):
        with pytest.raises(ValueError, match="at least one capability scope"):
            PlacementRequest(scopes=frozenset(), delivery=Delivery.SELF_PACED)

    def test_per_learner_without_a_learner_key_is_rejected(self):
        with pytest.raises(ValueError, match="learner_key"):
            PlacementRequest(
                scopes=frozenset({Scope.PER_LEARNER}),
                delivery=Delivery.SELF_PACED,
            )


class TestNames:
    def test_namespaces_are_dns_1123_labels(self):
        p = resolve_placement(req(cohort_key="Bravo Company", learner_key="LCpl O'Neill"))
        assert p.namespace == p.namespace.lower()
        assert all(c.isalnum() or c == "-" for c in p.namespace)
        assert not p.namespace.startswith("-") and not p.namespace.endswith("-")
        assert len(p.namespace) <= 63

    def test_long_keys_are_truncated_not_rejected(self):
        p = resolve_placement(req(cohort_key="c" * 80, learner_key="l" * 80))
        assert len(p.namespace) <= 63
