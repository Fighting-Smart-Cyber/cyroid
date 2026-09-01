"""One definition of what a VM runs, checked against every case the three
old copies handled.

The deploy path, the sync path and the pull list each worked this out
separately. They agreed on the easy cases and diverged on the rest, and every
divergence was a live defect: a disk-based golden image failing with "no
container image configured", a pinned runtime never fetched into the range
because the pull list resolved something else, a golden disk never cloned.

These cases are the union of what the three used to do.
"""

from proving_ground.models.base_image import BaseImage
from proving_ground.models.golden_image import GoldenImage
from proving_ground.models.snapshot import Snapshot
from proving_ground.models.vm import VM
from proving_ground.models.vm_enums import VMType
from proving_ground.services.image_resolution import ResolvedImage, resolve_vm_image

MIRROR = "172.30.0.16:5000"


def _vm(arch=None, base=None, golden=None, snap=None):
    vm = VM()
    vm.arch = arch
    vm.base_image = base
    vm.golden_image = golden
    vm.source_snapshot = snap
    return vm


class TestContainerBaseImage:
    def test_a_container_image_is_used_as_is(self):
        base = BaseImage(name="b", image_type="container", docker_image_tag="alpine:3.20")
        assert resolve_vm_image(_vm(base=base)).runtime == "alpine:3.20"

    def test_docker_image_id_is_the_fallback(self):
        base = BaseImage(name="b", image_type="container", docker_image_id="sha256:abc")
        assert resolve_vm_image(_vm(base=base)).runtime == "sha256:abc"

    def test_windows_is_pointed_at_the_right_architecture(self):
        """dockurr/windows and -arm are separate images, so the reference
        itself has to change; a platform flag cannot fix it."""
        base = BaseImage(
            name="b", image_type="container", docker_image_tag="dockurr/windows:latest"
        )
        assert (
            resolve_vm_image(_vm(arch="arm64", base=base)).runtime == "dockurr/windows-arm:latest"
        )
        assert resolve_vm_image(_vm(arch="x86_64", base=base)).runtime == "dockurr/windows:latest"

    def test_unknown_arch_leaves_the_reference_alone(self):
        """Guessing is how an arm host silently emulates an x86 image."""
        base = BaseImage(
            name="b", image_type="container", docker_image_tag="dockurr/windows:latest"
        )
        assert resolve_vm_image(_vm(base=base)).runtime == "dockurr/windows:latest"


class TestIsoBaseImage:
    def test_windows_iso_resolves_to_dockur(self):
        base = BaseImage(
            name="b", image_type="iso", vm_type=VMType.WINDOWS_VM, native_arch="x86_64"
        )
        assert resolve_vm_image(_vm(base=base)).runtime == "dockurr/windows:latest"

    def test_arm_comes_from_the_vm_then_the_image(self):
        base = BaseImage(name="b", image_type="iso", vm_type=VMType.WINDOWS_VM, native_arch="arm64")
        assert resolve_vm_image(_vm(base=base)).runtime == "dockurr/windows-arm:latest"
        assert resolve_vm_image(_vm(arch="x86_64", base=base)).runtime == "dockurr/windows:latest"

    def test_linux_and_macos(self):
        linux = BaseImage(name="b", image_type="iso", vm_type=VMType.LINUX_VM, native_arch="x86_64")
        mac = BaseImage(name="b", image_type="iso", vm_type=VMType.MACOS_VM, native_arch="x86_64")
        assert resolve_vm_image(_vm(base=linux)).runtime == "qemux/qemu:latest"
        assert resolve_vm_image(_vm(base=mac)).runtime == "dockurr/macos:latest"

    def test_an_iso_image_is_never_cloned(self):
        base = BaseImage(name="b", image_type="iso", vm_type=VMType.WINDOWS_VM)
        assert resolve_vm_image(_vm(base=base)).clone_from is None


class TestGoldenImage:
    def test_a_disk_based_image_resolves_its_pinned_runtime(self):
        """The defect: only docker_image_tag was read, and a captured image
        has none -- 'VM has no container image configured'."""
        g = GoldenImage(
            name="g",
            source="snapshot",
            vm_type=VMType.WINDOWS_VM,
            disk_image_path="/data/template-storage/win11",
            runtime_image_digest="dockurr/windows@sha256:0dfe075d",
        )
        r = resolve_vm_image(_vm(golden=g), mirror=MIRROR)
        assert r.runtime == f"{MIRROR}/dockurr/windows@sha256:0dfe075d"
        assert r.clone_from == "/data/template-storage/win11"

    def test_without_a_pin_it_falls_back_to_the_vm_type(self):
        """Images captured before pinning existed must still deploy."""
        g = GoldenImage(
            name="g",
            source="snapshot",
            vm_type=VMType.WINDOWS_VM,
            disk_image_path="/data/template-storage/win11",
            native_arch="x86_64",
        )
        r = resolve_vm_image(_vm(golden=g), mirror=MIRROR)
        assert r.runtime == "dockurr/windows:latest"
        assert r.clone_from == "/data/template-storage/win11"

    def test_a_container_golden_image_is_not_cloned_from_a_disk(self):
        g = GoldenImage(name="g", source="snapshot", docker_image_tag="myimg:1")
        r = resolve_vm_image(_vm(golden=g))
        assert r.runtime == "myimg:1"
        assert r.clone_from is None

    def test_no_mirror_leaves_the_digest_unqualified(self):
        g = GoldenImage(
            name="g",
            source="snapshot",
            vm_type=VMType.WINDOWS_VM,
            disk_image_path="/d",
            runtime_image_digest="dockurr/windows@sha256:abc",
        )
        assert resolve_vm_image(_vm(golden=g)).runtime == "dockurr/windows@sha256:abc"


class TestSnapshotAndEmpty:
    def test_a_snapshot_uses_its_tag(self):
        s = Snapshot(name="s", docker_image_tag="snap:1")
        assert resolve_vm_image(_vm(snap=s)).runtime == "snap:1"

    def test_a_vm_with_no_image_resolves_to_nothing(self):
        r = resolve_vm_image(_vm())
        assert r == ResolvedImage(runtime=None, clone_from=None, arch=None)

    def test_base_image_wins_over_golden(self):
        base = BaseImage(name="b", image_type="container", docker_image_tag="alpine:3.20")
        g = GoldenImage(name="g", source="snapshot", docker_image_tag="myimg:1")
        assert resolve_vm_image(_vm(base=base, golden=g)).runtime == "alpine:3.20"


class TestEmulationFlag:
    def test_qemu_runtimes_are_recognised(self):
        for image in (
            "dockurr/windows:latest",
            "qemux/qemu:latest",
            "dockurr/macos:latest",
            "172.30.0.16:5000/dockurr/windows@sha256:abc",
        ):
            assert ResolvedImage(runtime=image).is_emulated, image

    def test_a_plain_container_is_not_emulated(self):
        assert not ResolvedImage(runtime="alpine:3.20").is_emulated
        assert not ResolvedImage().is_emulated
