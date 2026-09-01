"""noVNC has to be told the right websocket path, whatever image the VM came from.

Two different servers are involved. A container VM runs KasmVNC, which builds
its socket URL as host + the full /vnc/{id} path. An emulated VM (dockur/QEMU)
runs noVNC's own websockify, reached at websockify relative to the console page.

The QEMU branch was gated on vm.base_image. A VM created from a golden image
has base_image_id NULL, so it fell through to the KasmVNC form and noVNC tried
to open a socket against the noVNC HTML page: "Failed to connect to server",
reconnecting forever. Only VMs deployed from a golden image were affected,
which is why it appeared as soon as that path started working.

Opening /vnc/{id}/ by hand hid the bug -- with no explicit ?path=, noVNC falls
back to websockify relative to the page, which is correct. Only the Console
button, which passes the path the API returns, showed it.
"""

import inspect

from proving_ground.api import vms as vms_api


# Real mapped images: a relationship accepts only a mapped object, and only
# of the right type -- vm.base_image will not take a GoldenImage.
def _base_img(vm_type):
    from proving_ground.models.base_image import BaseImage

    return BaseImage(name="b", vm_type=vm_type, os_type="linux")


def _golden_img(vm_type):
    from proving_ground.models.golden_image import GoldenImage

    return GoldenImage(name="g", source="snapshot", vm_type=vm_type, os_type="linux")


def _snap_img(vm_type):
    from proving_ground.models.snapshot import Snapshot

    return Snapshot(name="s", vm_type=vm_type, os_type="linux")


def _VM(base=None, golden=None, snap=None):
    """A real VM instance.

    These were duck-typed stubs, which stopped working once the resolvers
    began delegating to VM.effective_image -- a stub has the relationships but
    not the property. Building the real object keeps the test honest about
    what the code actually calls.
    """
    from proving_ground.models.vm import VM

    vm = VM()
    vm.base_image = base
    vm.golden_image = golden
    vm.source_snapshot = snap
    return vm


def test_type_comes_from_the_golden_image_when_there_is_no_base_image():
    vm = _VM(golden=_golden_img("windows_vm"))
    assert vms_api._vm_emulated_type(vm) == "windows_vm", (
        "A golden-image VM reports no vm_type, so it is treated as a container "
        "and noVNC is pointed at the wrong socket."
    )


def test_base_image_still_wins_when_present():
    vm = _VM(base=_base_img("linux_vm"), golden=_golden_img("windows_vm"))
    assert vms_api._vm_emulated_type(vm) == "linux_vm"


def test_a_plain_container_reports_no_emulated_type():
    assert vms_api._vm_emulated_type(_VM()) is None
    assert vms_api._vm_emulated_type(_VM(base=_base_img("container"))) == "container"
    assert vms_api._vm_emulated_type(_VM(base=_base_img("container"))) not in vms_api._QEMU_VM_TYPES


def test_every_websocket_path_branch_uses_the_resolver():
    """Both the DinD and the standard path decide this, and both must agree."""
    src = inspect.getsource(vms_api.get_vm_vnc_info)
    assert (
        "_vm_emulated_type" in src
    ), "vnc-info still decides the socket path from base_image alone."
    assert "vm.base_image.vm_type in (" not in src, (
        "A base_image-only vm_type check is back; a golden-image VM will be "
        "misrouted to the KasmVNC socket path again."
    )
    assert src.count("_vm_emulated_type(vm) in _QEMU_VM_TYPES") == 2, (
        "Both websocket_path branches (DinD and standard) must use the resolver; "
        f"found {src.count('_vm_emulated_type(vm) in _QEMU_VM_TYPES')}."
    )


class TestGoldenImageBlindSpots:
    """Branches that read vm.base_image alone mis-handle golden-image VMs.

    Found by sweeping for the pattern behind the console bug. Two more sites
    had it, both reached only by a VM created from a golden image -- which is
    why none of them surfaced until that deploy path started working.
    """

    def test_vnc_port_is_chosen_from_the_effective_image(self):
        """A golden image captured from a container VM is still a container.

        Reading base_image alone leaves it on 8006 (the QEMU port) with
        nothing listening, so the console cannot connect -- the same failure
        as the websocket path, one layer down.
        """
        src = inspect.getsource(vms_api.get_vm_vnc_info)
        assert "_vm_image_record(vm)" in src, "The VNC port is still chosen from base_image alone."
        assert (
            "if vm.base_image:\n                        # Check vm_type first" not in src
        ), "The base_image-only port branch is back."

    def test_the_resolver_prefers_base_then_golden_then_snapshot(self):
        base, golden, snap = (
            _base_img("container"),
            _golden_img("windows_vm"),
            _snap_img("linux_vm"),
        )
        assert vms_api._vm_image_record(_VM(base=base, golden=golden)) is base
        assert vms_api._vm_image_record(_VM(golden=golden, snap=snap)) is golden
        assert vms_api._vm_image_record(_VM(snap=snap)) is snap
        assert vms_api._vm_image_record(_VM()) is None


def test_snapshotting_a_golden_image_vm_keeps_its_type():
    """The label is load-bearing.

    The deploy paths choose dockurr/windows vs qemux/qemu from
    golden_image.vm_type, and the console picks its port and websocket path
    from it. A snapshot of a golden-image Windows VM that records
    'linux'/'container' produces a golden image that cannot boot.
    """
    import inspect as _inspect

    from proving_ground.api import snapshots as snap_api

    src = _inspect.getsource(snap_api)
    assert "vm.effective_vm_type" in src and "vm.effective_os_type" in src, (
        "Snapshot creation still derives os_type/vm_type itself instead of "
        "asking the VM, which is how a golden-image Windows VM was recorded "
        "as a linux container."
    )
