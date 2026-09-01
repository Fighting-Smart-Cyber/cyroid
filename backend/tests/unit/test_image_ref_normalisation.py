"""A stored image reference must mean the same thing on every deployment.

A RepoDigest read from a running container is qualified with whatever registry
it came from. Here that is the local mirror, so a captured golden image
recorded 172.30.0.16:5000/dockurr/windows@sha256:... -- correct on this host,
meaningless anywhere else.

It cost more than portability. Measured on one Windows deploy:

  * "Pool member ... image set does not cover required images; falling back to
    cold provisioning" -- the pool's label is written from tags, so a digest
    never matched and the warm container was thrown away.
  * The host pull failed ("server gave HTTP response to HTTPS client"), the
    pull fell back into DinD, and caching it back to the registry failed -- so
    every deploy repeated it.
"""

import inspect

from proving_ground.utils.image_ref import (
    repo_of,
    runtime_ref_for_pull,
    strip_registry,
    with_registry,
)

MIRROR = "172.30.0.16:5000"
PINNED = "172.30.0.16:5000/dockurr/windows@sha256:0dfe075d"
BARE = "dockurr/windows@sha256:0dfe075d"


class TestStripping:
    def test_a_mirror_qualified_digest_becomes_portable(self):
        assert strip_registry(PINNED) == BARE

    def test_a_namespace_is_not_mistaken_for_a_registry(self):
        """dockurr/ has no dot or colon, so it is a namespace, not a host."""
        assert strip_registry(BARE) == BARE
        assert strip_registry("dockurr/windows:latest") == "dockurr/windows:latest"

    def test_real_registries_are_recognised(self):
        assert strip_registry("ghcr.io/jongodb/cyroid-dind:latest") == "jongodb/cyroid-dind:latest"
        assert strip_registry("localhost:5000/foo/bar:1.2") == "foo/bar:1.2"

    def test_an_unqualified_image_is_untouched(self):
        assert strip_registry("alpine") == "alpine"
        assert strip_registry(None) is None


class TestRepoMatching:
    def test_a_digest_and_a_tag_share_a_repository(self):
        """This is what lets the warm pool match: a member holding
        dockurr/windows:latest can host a range wanting the pinned digest."""
        assert repo_of(PINNED) == repo_of("dockurr/windows:latest") == "dockurr/windows"

    def test_a_registry_port_is_not_read_as_a_tag(self):
        assert repo_of("localhost:5000/foo/bar:1.2") == "foo/bar"


class TestPullQualification:
    def test_storage_is_bare_but_pulling_is_qualified(self):
        """Air-gapped ranges can only reach the mirror, so a bare reference
        left to resolve against docker.io would not pull at all."""
        assert runtime_ref_for_pull(BARE, MIRROR) == PINNED

    def test_an_already_qualified_reference_is_not_double_prefixed(self):
        assert runtime_ref_for_pull(PINNED, MIRROR) == PINNED
        assert with_registry(PINNED, MIRROR).count("172.30.0.16") == 1

    def test_no_mirror_configured_leaves_the_reference_alone(self):
        assert runtime_ref_for_pull(BARE, None) == BARE


class TestWiredIn:
    def test_capture_stores_a_registry_agnostic_digest(self):
        from proving_ground.services.docker_service import DockerService

        src = inspect.getsource(DockerService.create_golden_image_from_range)
        assert "strip_registry" in src, (
            "Capture stores the RepoDigest as-is, so the reference is only "
            "valid on the host that produced it."
        )

    def test_the_pool_matches_on_repository(self):
        from proving_ground.services import range_pool_service

        src = inspect.getsource(range_pool_service)
        assert "repo_of(i) for i in required_images" in src, (
            "The pool still compares exact references, so a pinned digest "
            "never matches and Windows ranges cold-start."
        )

    def test_the_resolver_qualifies_with_the_mirror(self):
        """Storage is registry-agnostic; pulling needs somewhere to pull from.

        Asserted through the resolver's output rather than by grepping its
        source. This logic has moved twice -- out of three copies in
        range_deployment_service into resolve_vm_image, then again into
        runtime_for_image when the warm pool needed the same rules -- and a
        source-grep broke on each move without anything being wrong.
        """
        from proving_ground.models.golden_image import GoldenImage
        from proving_ground.models.vm_enums import VMType
        from proving_ground.services.image_resolution import runtime_for_image

        pinned = GoldenImage(
            name="g",
            source="snapshot",
            vm_type=VMType.WINDOWS_VM,
            disk_image_path="/d",
            runtime_image_digest=BARE,
        )
        assert runtime_for_image(pinned, mirror=MIRROR) == PINNED, (
            "The stored digest is handed back unqualified; an isolated range " "cannot pull it."
        )
        assert (
            runtime_for_image(pinned, mirror=None) == BARE
        ), "With no mirror configured the reference should be left alone."

    def test_the_deploy_service_supplies_the_mirror(self):
        """A resolver that can qualify is no use if callers omit the mirror."""
        from proving_ground.services import range_deployment_service

        src = inspect.getsource(range_deployment_service)
        assert "_REGISTRY_MIRROR" in src and "mirror=_REGISTRY_MIRROR" in src, (
            "The deploy paths call the resolver without a mirror, so a pinned "
            "digest stays unqualified and resolves against docker.io."
        )
