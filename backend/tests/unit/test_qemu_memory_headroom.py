"""An emulated VM needs memory for the emulator, not just for the guest.

dockur sets RAM_SIZE to the guest's allocation, and the VM container's cgroup
limit was being set to that same number -- so the guest was sized to the whole
cgroup and QEMU itself had nothing left.

Observed live: a 4096 MiB guest in a 4096 MiB container was OOM-killed by its
memcg and restarted roughly every 40 seconds, 46 times, with the VNC console
unreachable throughout. A 16384 MiB VM on the same host survived, because
dockur clamps the guest to ~90% of what it sees -- which leaves ~1.5 GB at
16 GB but only ~0.4 GB at 4 GB. The emulator's overhead is roughly fixed, so
small VMs are the ones that fail.
"""

import inspect
import re

from proving_ground.services import range_deployment_service


def test_the_container_gets_more_memory_than_the_guest():
    from proving_ground.services.range_deployment_service import _vm_container_memory_mb

    for guest in (2048, 4096, 16384):
        limit = _vm_container_memory_mb(guest)
        assert limit > guest, (
            f"A {guest} MiB guest in a {limit} MiB container leaves the emulator "
            f"nothing; the memcg kills it and the VM restart-loops."
        )
        # Measured, not guessed: a 4 GiB guest settles at 4.91 GiB, so QEMU
        # costs ~0.9 GiB and the boot peak is higher. A 1 GiB allowance was
        # tried live and still OOM-killed once on the way up, then sat at 98%
        # of its limit -- surviving by margin of error, not by design.
        assert limit - guest >= 1536, (
            f"Only {limit - guest} MiB of headroom for QEMU at guest={guest}; "
            f"measured overhead is ~0.9-1.1 GiB steady state and higher at boot."
        )


def test_headroom_is_not_proportional():
    """A percentage would shrink exactly where it is already too small."""
    from proving_ground.services.range_deployment_service import _vm_container_memory_mb

    small = _vm_container_memory_mb(2048) - 2048
    large = _vm_container_memory_mb(16384) - 16384
    assert small == large, (
        "Headroom scales with guest size, so a small VM gets the least -- but "
        "the emulator's overhead does not shrink with the guest."
    )


def _fn_src(name: str) -> str:
    return inspect.getsource(getattr(range_deployment_service.RangeDeploymentService, name))


def test_both_deploy_paths_apply_the_headroom():
    for name in ("_deploy_with_dind", "sync_range"):
        src = _fn_src(name)
        assert (
            "_vm_container_memory_mb" in src
        ), f"{name} still sizes the VM container to the guest's RAM exactly."
        assert not re.search(
            r"memory_limit_mb=vm\.ram_mb\b", src
        ), f"{name} still passes vm.ram_mb straight through as the container limit."


def test_plain_containers_do_not_get_vm_headroom():
    """A container VM runs no emulator, so the extra GiB would just be waste."""
    for name in ("_deploy_with_dind", "sync_range"):
        src = _fn_src(name)
        idx = src.find("_vm_container_memory_mb")
        # Wide enough to span the whole conditional expression. The call grew
        # multi-line when it gained the ISO-install allowance, which pushed
        # "if privileged" out of a 300-char window while the guard it checks
        # for was still there -- a window too tight fails on formatting.
        window = src[max(0, idx - 200) : idx + 800]
        assert "privileged" in window, (
            f"{name} applies the emulator headroom unconditionally; only "
            f"privileged (QEMU/dockur) VMs need it."
        )
