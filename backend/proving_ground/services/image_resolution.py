"""One answer to "what does this VM run, and from what disk?".

Three places used to work this out independently -- the deploy path, the sync
path, and the pull list that decides which images get fetched into a range.
They agreed on the easy cases and diverged on the rest, and every divergence
was a live defect:

  * deploy and sync each read only docker_image_tag, so a disk-based golden
    image (which has none) failed with "no container image configured";
  * the pull list resolved a different image from the deploy, so a pinned
    runtime was never fetched and container creation failed on an image the
    range did not have;
  * only one of them knew a golden image's disk has to be cloned at all.

Each was fixed in isolation, in the site where it happened to surface. This is
the definition all three now share.
"""

from dataclasses import dataclass
from typing import Optional

from proving_ground.models.vm_enums import VMType
from proving_ground.utils.image_ref import runtime_ref_for_pull

# dockurr/windows and dockurr/windows-arm are separate images, not one image
# with two architectures, so an arch mismatch cannot be fixed by a platform
# flag -- the reference itself has to change.
_WINDOWS_X86 = "dockurr/windows:latest"
_WINDOWS_ARM = "dockurr/windows-arm:latest"
_MACOS = "dockurr/macos:latest"
_QEMU = "qemux/qemu:latest"


@dataclass(frozen=True)
class ResolvedImage:
    """What a VM needs in order to run.

    runtime:    the container image to pull and run
    clone_from: a golden image's disk directory, when the VM boots from one
    arch:       the architecture the pull should request, if any
    """

    runtime: Optional[str] = None
    clone_from: Optional[str] = None
    arch: Optional[str] = None

    @property
    def is_emulated(self) -> bool:
        """Whether the runtime is a QEMU/dockur VM rather than a container."""
        if not self.runtime:
            return False
        lowered = self.runtime.lower()
        return any(
            k in lowered
            for k in ("dockurr/windows", "dockur/windows", "qemux/qemu", "dockurr/macos")
        )


def _emulated_runtime(vm_type, target_arch: Optional[str]) -> str:
    """The QEMU/dockur image that runs a VM of this type."""
    if vm_type == VMType.WINDOWS_VM:
        return _WINDOWS_ARM if target_arch == "arm64" else _WINDOWS_X86
    if vm_type == VMType.MACOS_VM:
        return _MACOS
    return _QEMU


def _apply_windows_arch(image: str, arch: Optional[str]) -> str:
    """Point a dockur Windows reference at the right architecture's image."""
    lowered = image.lower()
    if "dockurr/windows" not in lowered and "dockur/windows" not in lowered:
        return image
    if arch == "x86_64":
        return _WINDOWS_X86
    if arch == "arm64":
        return _WINDOWS_ARM
    # arch unknown: leave the reference alone rather than guess wrong. The
    # caller logs this; guessing here is how an arm host ends up running an
    # x86 image under emulation without anyone noticing.
    return image


def runtime_for_image(
    image, arch: Optional[str] = None, mirror: Optional[str] = None
) -> Optional[str]:
    """The runtime image for an image record, without needing a VM.

    The warm pool holds image records from blueprints rather than VM rows, and
    it used to read docker_image_tag alone -- which a disk-based golden image
    does not have. So a Windows golden image contributed nothing to the set the
    pool pre-pulls, no member ever matched, and every Windows range fell back
    to cold provisioning.
    """
    if image is None:
        return None
    tag = getattr(image, "docker_image_tag", None) or getattr(image, "docker_image_id", None)
    if tag:
        return _apply_windows_arch(tag, arch)

    vm_type = getattr(image, "vm_type", None)
    target_arch = arch or getattr(image, "native_arch", None)

    if getattr(image, "image_type", None) == "iso":
        return _emulated_runtime(vm_type, target_arch)

    if getattr(image, "disk_image_path", None):
        pinned = runtime_ref_for_pull(getattr(image, "runtime_image_digest", None), mirror)
        return pinned or _emulated_runtime(vm_type, target_arch)

    return None


def resolve_vm_image(vm, mirror: Optional[str] = None) -> ResolvedImage:
    """Resolve the runtime image and clone source for a VM.

    Reads whichever image the VM actually came from (VM.effective_image), so a
    golden-image VM is not mistaken for a plain container. The runtime rules
    themselves live in runtime_for_image, which the warm pool also uses -- this
    adds only what needs a VM: the clone source and the requested arch.
    """
    image = vm.effective_image
    if image is None:
        return ResolvedImage(arch=vm.arch)

    # An ISO base image boots an installer, so there is nothing to clone. Any
    # other image carrying a disk is cloned from it.
    clone_from = (
        None
        if getattr(image, "image_type", None) == "iso"
        else getattr(image, "disk_image_path", None)
    )
    return ResolvedImage(
        runtime=runtime_for_image(image, arch=vm.arch, mirror=mirror),
        clone_from=clone_from,
        arch=vm.arch,
    )
