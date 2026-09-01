"""The pool has to pre-pull what golden-image ranges actually run.

Measured before this: a Windows range deployed from a golden image logged

    Pool member 67ab34453211 image set does not cover required images;
    falling back to cold provisioning

and paid ~17s to build a DinD container that was already sitting warm. Two
separate reasons, and !54 only fixed the first:

  1. the pool matched exact references, so a pinned digest never matched a
     tag-derived label -- fixed by comparing repositories;
  2. the pool's label never contained the Windows runtime at all, because
     the golden-image branch read docker_image_tag alone and a disk-based
     golden image has none.

This covers the second.
"""

import inspect

from proving_ground.models.golden_image import GoldenImage
from proving_ground.models.base_image import BaseImage
from proving_ground.models.vm_enums import VMType
from proving_ground.services.image_resolution import runtime_for_image

MIRROR = "172.30.0.16:5000"


class TestRuntimeForImage:
    def test_a_pinned_disk_image_resolves_to_its_runtime(self):
        g = GoldenImage(
            name="g",
            source="snapshot",
            vm_type=VMType.WINDOWS_VM,
            disk_image_path="/d",
            runtime_image_digest="dockurr/windows@sha256:abc",
        )
        assert runtime_for_image(g, mirror=MIRROR) == f"{MIRROR}/dockurr/windows@sha256:abc"

    def test_an_unpinned_disk_image_falls_back_to_the_vm_type(self):
        g = GoldenImage(
            name="g",
            source="snapshot",
            vm_type=VMType.WINDOWS_VM,
            disk_image_path="/d",
            native_arch="x86_64",
        )
        assert runtime_for_image(g) == "dockurr/windows:latest"

    def test_a_container_golden_image_uses_its_tag(self):
        g = GoldenImage(name="g", source="snapshot", docker_image_tag="myimg:1")
        assert runtime_for_image(g) == "myimg:1"

    def test_an_image_with_nothing_to_run_resolves_to_none(self):
        assert runtime_for_image(GoldenImage(name="g", source="snapshot")) is None
        assert runtime_for_image(None) is None

    def test_an_iso_base_image_resolves_to_its_emulator(self):
        b = BaseImage(name="b", image_type="iso", vm_type=VMType.LINUX_VM, native_arch="x86_64")
        assert runtime_for_image(b) == "qemux/qemu:latest"

    def test_arch_selects_the_right_windows_image(self):
        b = BaseImage(name="b", image_type="container", docker_image_tag="dockurr/windows:latest")
        assert runtime_for_image(b, arch="arm64") == "dockurr/windows-arm:latest"


class TestPoolUsesIt:
    def _src(self):
        from proving_ground.services import range_pool_service

        return inspect.getsource(range_pool_service)

    def test_the_blueprint_branch_uses_the_shared_rules(self):
        src = self._src()
        assert "runtime_for_image(golden_img, arch=arch" in src, (
            "The pool still reads docker_image_tag alone for golden images, so "
            "a disk-based image contributes nothing to pre-pull."
        )
        assert "golden_img.docker_image_tag or golden_img.docker_image_id" not in src

    def test_golden_images_outside_blueprints_are_included(self):
        """Capturing an image and deploying straight from it is the normal
        flow; it never goes near a blueprint."""
        src = self._src()
        assert "for golden_img in golden_images.values()" in src, (
            "Only blueprint-referenced images are pre-pulled, so a range built "
            "directly on a captured image still cold-starts."
        )

    def test_the_mirror_is_applied(self):
        src = self._src()
        assert "_REGISTRY_MIRROR" in src, (
            "Without the mirror the pinned digest is unqualified and will not "
            "match what the deploy path pulls."
        )
