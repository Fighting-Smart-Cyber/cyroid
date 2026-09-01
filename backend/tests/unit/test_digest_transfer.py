"""A digest reference is transferred by tar, not pushed to the registry.

Observed on every pinned Windows deploy:

    GET /v2/172.30.0.16:5000/dockurr/windows@sha256/tags/list -> 404
    ERROR Docker API error pushing 172.30.0.16:5000/dockurr/windows@sha256:0dfe...
    WARNING Failed to push to registry, falling back to tar

get_registry_tag() splits on the last colon, so repo@sha256:0dfe... becomes the
repository "repo@sha256" with the tag "0dfe...". There is no such repository
and there never can be -- a digest is not a tag. The transfer then falls
through to tar and completes, so nothing broke; it just spent a round trip on
an attempt that cannot succeed and logged an ERROR that reads like a registry
fault. ADR-0007 makes digest pinning the rule, so this was the path every
image would come to take.
"""

import inspect

from proving_ground.services import docker_service
from proving_ground.utils.image_ref import is_digest_ref, repo_of


class TestThepredicate:
    def test_a_digest_reference_is_recognised(self):
        assert is_digest_ref("dockurr/windows@sha256:0dfe075dd7e4ce27")
        assert is_digest_ref("172.30.0.16:5000/dockurr/windows@sha256:0dfe075dd7e4ce27")

    def test_a_tag_is_not_a_digest(self):
        assert not is_digest_ref("dockurr/windows:latest")
        assert not is_digest_ref("172.30.0.16:5000/dockurr/windows:latest")

    def test_empty_input_is_not_a_digest(self):
        assert not is_digest_ref(None)
        assert not is_digest_ref("")

    def test_it_agrees_with_repo_of_on_the_reference_that_caused_this(self):
        """The bug was a repository name of "<repo>@sha256"; repo_of is what a
        correct split looks like."""
        ref = "172.30.0.16:5000/dockurr/windows@sha256:0dfe075dd7e4ce27"
        assert repo_of(ref) == "dockurr/windows"
        assert "@sha256" not in repo_of(ref)


class TestTheTransferSkipsTheRegistryForDigests:
    def test_the_registry_route_is_guarded_by_the_predicate(self):
        src = inspect.getsource(docker_service.DockerService.transfer_image_to_dind)
        assert "is_digest_ref(image)" in src, (
            "The transfer still attempts a registry push for digest references, "
            "which cannot succeed."
        )

    def test_every_registry_entry_point_is_guarded(self):
        """There are two. Guarding only the second one was the first attempt at
        this fix: the earlier registry-first block is already dead for a digest
        (its repo split yields "<repo>@sha256", which no catalog name matches)
        but still spends a list_images() round trip getting there."""
        src = inspect.getsource(docker_service.DockerService.transfer_image_to_dind)
        assert src.count("is_digest_ref(image)") == src.count("registry.is_healthy()"), (
            "A registry entry point has no digest guard; a pinned image still "
            "reaches the registry there."
        )

    def test_the_guard_precedes_the_first_health_check(self):
        src = inspect.getsource(docker_service.DockerService.transfer_image_to_dind)
        assert src.index("is_digest_ref(image)") < src.index(
            "registry.is_healthy()"
        ), "The health check runs first, so the registry is still consulted."

    def test_the_push_branch_is_excluded_not_merely_preceded(self):
        """`elif` is what skips the push; a bare `if` would run both."""
        src = inspect.getsource(docker_service.DockerService.transfer_image_to_dind)
        assert "elif await registry.is_healthy():" in src

    def test_tagged_images_still_take_the_registry_route(self):
        """The registry transfer is a real optimisation for tagged images and
        must not be collateral damage of this fix."""
        src = inspect.getsource(docker_service.DockerService.transfer_image_to_dind)
        assert "ensure_image_in_registry" in src
