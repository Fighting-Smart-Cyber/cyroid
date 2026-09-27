"""What an install says it can do, per substrate.

The point of these is that the policy has exactly one home. If a feature ever has to be gated
differently, it changes here and the UI follows -- nothing in the frontend re-derives it.
"""

import pytest

from proving_ground.api.capabilities import capabilities_for

DOCKER_ONLY = [
    "image_cache",
    "docker_registry",
    "image_library",
    "docker_status",
    "range_composition",
    "legacy_range_console",
]
KUBERNETES_ONLY = ["workloads", "range_apps"]
ALWAYS = ["blueprints", "training_events", "content_library", "catalog"]


class TestKubernetes:
    @pytest.mark.parametrize("feature", DOCKER_ONLY)
    def test_nothing_built_on_the_docker_socket_is_offered(self, feature):
        # A Kubernetes install has no Docker socket at all; offering these produced the
        # "Failed to load cache data" / registry Unhealthy pages a user met on 0.51.0.
        assert getattr(capabilities_for("kubernetes").features, feature) is False

    @pytest.mark.parametrize("feature", KUBERNETES_ONLY)
    def test_the_substrates_own_surfaces_are_offered(self, feature):
        assert getattr(capabilities_for("kubernetes").features, feature) is True

    def test_it_names_itself_for_an_operator(self):
        caps = capabilities_for("kubernetes")
        assert (caps.substrate, caps.substrate_label) == ("kubernetes", "Kubernetes")


class TestDocker:
    @pytest.mark.parametrize("feature", DOCKER_ONLY)
    def test_the_frozen_path_keeps_everything_it_had(self, feature):
        # Era A is frozen, not changed: a Docker host must look exactly as it did.
        assert getattr(capabilities_for("dind").features, feature) is True

    @pytest.mark.parametrize("feature", KUBERNETES_ONLY)
    def test_kubernetes_only_surfaces_are_hidden(self, feature):
        assert getattr(capabilities_for("dind").features, feature) is False

    def test_an_unset_or_unknown_substrate_is_treated_as_docker(self):
        # `range_substrate` defaults to "dind" and a host that sets nothing keeps what it had;
        # an unknown value must not silently unlock the Kubernetes surfaces.
        for value in ("", "dind", "something-else"):
            caps = capabilities_for(value)
            assert caps.features.workloads is False
            assert caps.features.image_cache is True


@pytest.mark.parametrize("feature", ALWAYS)
@pytest.mark.parametrize("substrate", ["kubernetes", "dind"])
def test_era_neutral_surfaces_are_offered_on_both(substrate, feature):
    assert getattr(capabilities_for(substrate).features, feature) is True


def test_every_feature_is_classified_by_these_tests():
    """A new flag must be added to one of the three lists above, or it is untested."""
    declared = set(capabilities_for("dind").features.model_dump())
    assert declared == set(DOCKER_ONLY) | set(KUBERNETES_ONLY) | set(ALWAYS)
