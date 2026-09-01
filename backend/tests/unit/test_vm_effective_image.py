"""One definition of "which image is this VM from".

That question was answered independently at a dozen call sites, each reaching
for vm.base_image and so treating a golden-image VM as a plain container. Every
one was a live defect before it was found:

    deploy      "VM has no container image configured"
    sync        the same, plus UnboundLocalError on base_img
    pull list   the pinned runtime was never fetched into the range
    console     noVNC pointed at the HTML page instead of websockify
    console     the QEMU port used for a container VM
    snapshots   a Windows VM recorded as a linux container

The property does not prevent someone writing vm.base_image again, but it
removes the reason to.
"""

import inspect

from proving_ground.models.base_image import BaseImage
from proving_ground.models.golden_image import GoldenImage
from proving_ground.models.snapshot import Snapshot
from proving_ground.models.vm import VM


# Real mapped instances throughout: a relationship only accepts a mapped
# object, so a stub cannot be assigned to vm.base_image at all.
def _base(vm_type=None, os_type=None):
    return BaseImage(name="b", vm_type=vm_type, os_type=os_type)


def _golden(vm_type=None, os_type=None):
    return GoldenImage(name="g", source="snapshot", vm_type=vm_type, os_type=os_type)


def _snap(vm_type=None, os_type=None):
    return Snapshot(name="s", vm_type=vm_type, os_type=os_type)


def _vm(base=None, golden=None, snap=None):
    vm = VM()
    vm.base_image = base
    vm.golden_image = golden
    vm.source_snapshot = snap
    return vm


class TestPrecedence:
    def test_base_image_wins(self):
        base, golden = _base("container"), _golden("windows_vm")
        assert _vm(base=base, golden=golden).effective_image is base

    def test_golden_image_when_there_is_no_base(self):
        """The case behind every defect listed above."""
        golden = _golden("windows_vm")
        assert _vm(golden=golden).effective_image is golden

    def test_source_snapshot_last(self):
        snap = _snap("linux_vm")
        assert _vm(snap=snap).effective_image is snap

    def test_none_when_the_vm_has_no_image(self):
        assert _vm().effective_image is None


class TestDerivedFields:
    def test_vm_type_follows_the_effective_image(self):
        assert _vm(golden=_golden(vm_type="windows_vm")).effective_vm_type == "windows_vm"
        assert _vm().effective_vm_type is None

    def test_os_type_follows_the_effective_image(self):
        assert _vm(golden=_golden(os_type="windows")).effective_os_type == "windows"
        assert _vm().effective_os_type is None

    def test_unset_columns_do_not_raise(self):
        """An image row whose type columns were never populated."""
        vm = _vm(snap=_snap())
        assert vm.effective_vm_type is None
        assert vm.effective_os_type is None


class TestCallSitesUseIt:
    def test_the_console_resolvers_delegate(self):
        from proving_ground.api import vms as vms_api

        assert "vm.effective_image" in inspect.getsource(vms_api._vm_image_record)
        assert "vm.effective_vm_type" in inspect.getsource(vms_api._vm_emulated_type)

    def test_snapshot_labelling_delegates(self):
        from proving_ground.api import snapshots as snap_api

        src = inspect.getsource(snap_api)
        assert "vm.effective_vm_type" in src and "vm.effective_os_type" in src, (
            "Snapshot creation still derives os_type/vm_type itself, which is "
            "how a golden-image Windows VM got recorded as a linux container."
        )
