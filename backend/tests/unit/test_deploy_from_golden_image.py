"""Deploying a Windows range from a captured golden image.

A golden image captured from a dockur VM is a DISK, not a container image: it
has a disk_image_path and no docker_image_tag. _deploy_with_dind only ever read
the tag, so such a VM failed with "has no container image configured" -- and
because partial VM failures are collected rather than raised, the range still
reported "deployed successfully".

The clone itself was missing too. range_deployment_service had no clone_from,
no disk_image_path and no storage handling of any kind; the only clone code
lived on the legacy host path, which cannot see inside a DinD range. So a
golden image was a database row that no range could ever boot from.

These guard both halves.
"""

import inspect

from proving_ground.services import range_deployment_service


def _deploy_src() -> str:
    return inspect.getsource(range_deployment_service.RangeDeploymentService._deploy_with_dind)


def test_disk_based_golden_image_resolves_a_runtime_image():
    """The regression: only docker_image_tag was consulted, so a captured
    Windows image deployed to "no container image configured".

    The rules now live in image_resolution.resolve_vm_image and are exercised
    directly in test_image_resolution.py; this asserts the deploy path uses
    them rather than reimplementing them.
    """
    src = _deploy_src()
    assert "resolve_vm_image(" in src, (
        "_deploy_with_dind resolves the image itself again, which is how it "
        "and the pull list drifted apart."
    )


def test_the_golden_disk_is_actually_cloned():
    """A runtime image alone boots an empty disk -- the captured storage has
    to be copied into the VM's own /storage."""
    src = _deploy_src()
    assert "_copytree_sparse" in src, (
        "The golden image's disk is never cloned into the VM's storage, so the "
        "VM boots blank. (It must also be the sparse copy: a 9 GB image would "
        "otherwise inflate to its full 64 GB apparent size per range.)"
    )
    assert (
        'bind": "/storage"' in src or "'/storage'" in src
    ), "The cloned storage is never bind-mounted into the VM."


def test_golden_image_and_installer_iso_are_mutually_exclusive():
    """Mounting the ISO alongside a cloned disk reinstalls Windows over it."""
    src = _deploy_src()
    gidx = src.find("golden_src and os.path.isdir(golden_src)")
    assert gidx != -1, "No branch distinguishing a golden-image boot from an ISO boot."
    tail = src[gidx:]
    assert "else:" in tail, (
        "The cached-ISO fallback must sit in the else branch; mounting /boot.iso "
        "on a cloned disk starts a fresh install over the top of it."
    )


def test_clone_target_is_under_vm_storage():
    """vm-storage is bind-mounted into the DinD daemon; a path outside it does
    not resolve inside the range."""
    src = _deploy_src()
    assert "vm_storage_dir" in src, (
        "The clone must land under settings.vm_storage_dir, which is bind-mounted "
        "into the range's daemon. Anywhere else is invisible inside the range."
    )


def test_existing_storage_is_not_clobbered():
    """Re-deploying a range must not wipe a running VM's disk."""
    src = _deploy_src()
    gidx = src.find("golden_src and os.path.isdir(golden_src)")
    tail = src[gidx : gidx + 1200]
    assert "_has_usable_disk(vm_storage)" in tail, (
        "The clone overwrites unconditionally; a re-deploy would discard "
        "whatever the learner had done in the VM."
    )


class TestSyncRangeHasTheSameSupport:
    """sync_range provisions VMs into an ALREADY-RUNNING range.

    It is a separate implementation from _deploy_with_dind and had the same
    gap: it read docker_image_tag and never disk_image_path, and did no
    cloning. Fixing only the deploy path leaves "add a golden-image VM to a
    running range" broken -- which is the mirror of the cached-ISO bug, where
    sync had the handling and deploy did not.
    """

    def _sync_src(self) -> str:
        return inspect.getsource(range_deployment_service.RangeDeploymentService.sync_range)

    def test_sync_resolves_a_disk_based_golden_image(self):
        """sync_range must resolve the same way the deploy path does.

        It used to reimplement the rules and read only docker_image_tag, so
        provisioning a captured golden image into a running range had no
        runtime image at all.
        """
        src = self._sync_src()
        assert "resolve_vm_image(" in src, (
            "sync_range resolves the image itself again; it and the deploy "
            "path will drift, which is how this broke the first time."
        )

    def test_sync_clones_the_golden_disk(self):
        src = self._sync_src()
        assert (
            "_copytree_sparse" in src
        ), "sync_range does not clone the golden disk, so the VM boots blank."

    def test_sync_does_not_mount_the_installer_over_a_clone(self):
        src = self._sync_src()
        idx = src.find("golden_clone_src and os.path.isdir(golden_clone_src)")
        assert idx != -1, "sync_range has no golden-vs-ISO branch."
        assert "else:" in src[idx:], (
            "The ISO fallback must be in the else branch; mounting /boot.iso on "
            "a cloned disk reinstalls Windows over it."
        )

    def test_base_img_is_defined_on_every_path(self):
        """base_img was read unconditionally but assigned only for base images.

        A golden-image VM hit UnboundLocalError: "cannot access local variable
        'base_img'". It is now assigned unconditionally from the VM, so the
        error is structurally impossible rather than merely avoided.
        """
        src = self._sync_src()
        assert "base_img = vm.base_image" in src, (
            "base_img is conditionally assigned again, so a VM created from a "
            "golden image can reach a read before the assignment."
        )


class TestRuntimeIsPinnedAtCaptureTime:
    """A captured disk expects the virtual hardware it was installed on.

    dockur is pulled by the :latest tag, so it drifts. Observed live: a range
    that cached the image four weeks earlier ran QEMU 10.0.11 and booted the
    original disk straight to the desktop; a range created the same day pulled
    a newer build running QEMU 11.1.0, and the clone of that same disk went
    into "Preparing Automatic Repair" before it came up. Recording the digest
    at capture time is what makes a golden image reproducible, and it is what
    ADR-0007 asks for -- everything by digest, no floating tags at runtime.
    """

    def test_capture_records_the_runtime_digest(self):
        from proving_ground.services.docker_service import DockerService

        src = inspect.getsource(DockerService.create_golden_image_from_range)
        assert "RepoDigests" in src, (
            "Capture does not resolve the runtime image's digest, so the "
            "captured disk has no record of the hardware it was built on."
        )
        assert "runtime_image_digest" in src

    def test_the_model_can_store_it(self):
        from proving_ground.models.golden_image import GoldenImage

        assert hasattr(
            GoldenImage, "runtime_image_digest"
        ), "GoldenImage cannot record the runtime it was captured against."

    def test_capture_survives_an_unresolvable_digest(self):
        """Pinning is best effort: an image with no RepoDigest (built or
        side-loaded) must not fail the capture."""
        from proving_ground.services.docker_service import DockerService

        src = inspect.getsource(DockerService.create_golden_image_from_range)
        idx = src.find("RepoDigests")
        assert "except Exception" in src[idx:], (
            "A capture must not fail because the runtime digest could not be "
            "resolved -- the disk is still perfectly usable."
        )

    def test_all_three_resolution_sites_share_one_definition(self):
        """Deploy, sync and the pull-list must resolve identically.

        The pull list matters most: if it resolves a different image from the
        deploy, the pinned runtime is never pulled into the range and container
        creation fails on an image the range does not have. They now call the
        same function instead of each implementing the rules.
        """
        for name in ("_deploy_with_dind", "sync_range", "_resolve_range_images"):
            src = inspect.getsource(getattr(range_deployment_service.RangeDeploymentService, name))
            assert (
                "resolve_vm_image(" in src
            ), f"{name} still resolves the image itself; the three will drift."

    def test_an_unpinned_image_still_deploys(self):
        """Images captured before pinning existed have no digest and must keep
        working via the vm_type fallback.

        Asserted behaviourally rather than by grepping for the helper: it has
        moved once already, from resolve_vm_image into runtime_for_image when
        the warm pool needed the same rules.
        """
        from proving_ground.models.golden_image import GoldenImage
        from proving_ground.models.vm_enums import VMType
        from proving_ground.services.image_resolution import runtime_for_image

        unpinned = GoldenImage(
            name="g",
            source="snapshot",
            vm_type=VMType.WINDOWS_VM,
            disk_image_path="/data/template-storage/win11",
            native_arch="x86_64",
        )
        assert runtime_for_image(unpinned) == "dockurr/windows:latest", (
            "A golden image with no recorded runtime has no fallback, so every "
            "image captured before pinning stops deploying."
        )

    def test_the_clone_happens_even_when_a_runtime_is_pinned(self):
        """The clone source must not depend on how the runtime resolved.

        sync_range once set it inside the "no image_tag" branch, so recording a
        digest -- which makes image_tag resolve immediately -- would skip that
        branch and leave the VM booting the right runtime against an empty
        disk. The resolver now returns both together, from the image itself.
        """
        from proving_ground.models.golden_image import GoldenImage
        from proving_ground.models.vm import VM
        from proving_ground.models.vm_enums import VMType
        from proving_ground.services.image_resolution import resolve_vm_image

        golden = GoldenImage(
            name="g",
            source="snapshot",
            vm_type=VMType.WINDOWS_VM,
            disk_image_path="/data/template-storage/win11",
            runtime_image_digest="dockurr/windows@sha256:abc",
        )
        vm = VM()
        vm.arch = None
        vm.base_image = None
        vm.golden_image = golden
        vm.source_snapshot = None

        resolved = resolve_vm_image(vm, mirror="172.30.0.16:5000")
        assert resolved.runtime, "a pinned runtime should resolve"
        assert resolved.clone_from == "/data/template-storage/win11", (
            "The disk is not cloned when a runtime is pinned, so the VM boots "
            "the right image against an empty disk."
        )
