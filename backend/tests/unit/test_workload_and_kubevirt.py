"""Typed workload spec and its KubeVirt translation -- COSMOS PG-42.

The point of PG-42 is that ~30 flat provider columns on `VM` stop being the domain model. These
pin the replacement: what the spec accepts, what it refuses, and what comes out the other side.
"""

import pytest

from proving_ground.capability.kubevirt import virtual_machine_manifest
from proving_ground.capability.models import Isolation, Placement
from proving_ground.capability.networking import NetworkSpec
from proving_ground.capability.workload import (
    DiskSpec,
    InterfaceSpec,
    OSFamily,
    WorkloadSpec,
    workload_from_vm_row,
)

NETS = [
    NetworkSpec(name="dmz", subnet="172.30.10.0/24", gateway="172.30.10.1"),
    NetworkSpec(name="internal", subnet="172.30.20.0/24"),
]
PLACEMENT = Placement(isolation=Isolation.NAMESPACE, namespace="pg-alpha-jones", reason="t")


def row(**kw):
    base = dict(
        hostname="dc01",
        cpu=4,
        ram_mb=8192,
        disk_gb=80,
        windows_version="2022",
        interfaces=[InterfaceSpec(network="dmz", ip_address="172.30.10.5", primary=True)],
    )
    base.update(kw)
    return workload_from_vm_row(**base)


class TestTheFlatColumnsCollapse:
    def test_windows_becomes_an_os_family_and_version(self):
        w = row()
        assert w.os_family is OSFamily.WINDOWS and w.os_version == "2022"

    def test_linux_and_macos_too(self):
        assert row(windows_version=None, linux_distro="ubuntu").os_family is OSFamily.LINUX
        assert row(windows_version=None, macos_version="sonoma").os_family is OSFamily.MACOS

    def test_extra_disk_columns_become_disks(self):
        w = row(disk2_gb=100, disk3_gb=250)
        assert [d.size_gb for d in w.disks] == [80, 100, 250]
        assert sum(d.boot for d in w.disks) == 1

    def test_no_operating_system_is_refused(self):
        with pytest.raises(ValueError, match="declares no operating system"):
            row(windows_version=None)

    def test_two_operating_systems_are_refused(self):
        # Not a dual-boot machine -- a bug that today resolves to whichever `if` runs first.
        with pytest.raises(ValueError, match="more than one operating system"):
            row(linux_distro="ubuntu")

    def test_a_workload_on_no_network_is_refused(self):
        with pytest.raises(ValueError, match="at least one network interface"):
            row(interfaces=[])


class TestSpecInvariants:
    def test_exactly_one_boot_disk(self):
        with pytest.raises(ValueError, match="exactly one boot disk"):
            WorkloadSpec(
                name="x",
                os_family=OSFamily.LINUX,
                os_version="1",
                cpus=1,
                memory_mb=512,
                disks=(DiskSpec("a", 10, boot=True), DiskSpec("b", 10, boot=True)),
                interfaces=(InterfaceSpec(network="dmz", ip_address="172.30.10.5"),),
            )

    def test_a_zero_sized_disk_is_refused(self):
        with pytest.raises(ValueError, match="larger than zero"):
            DiskSpec("root", 0, boot=True)

    def test_primary_defaults_to_the_first_interface(self):
        w = row(
            interfaces=[
                InterfaceSpec(network="dmz", ip_address="172.30.10.5"),
                InterfaceSpec(network="internal", ip_address="172.30.20.5"),
            ]
        )
        assert w.primary_interface.network == "dmz"


class TestKubeVirtManifest:
    def test_resources_land_on_the_domain(self):
        m = virtual_machine_manifest(row(), PLACEMENT, NETS)
        domain = m["spec"]["template"]["spec"]["domain"]
        assert domain["cpu"]["cores"] == 4
        assert domain["memory"]["guest"] == "8192Mi"

    def test_only_the_boot_disk_gets_a_boot_order(self):
        m = virtual_machine_manifest(row(disk2_gb=50), PLACEMENT, NETS)
        disks = m["spec"]["template"]["spec"]["domain"]["devices"]["disks"]
        assert [d.get("bootOrder") for d in disks] == [1, None]

    def test_windows_gets_sata_and_linux_gets_virtio(self):
        win = virtual_machine_manifest(row(), PLACEMENT, NETS)
        lin = virtual_machine_manifest(
            row(windows_version=None, linux_distro="ubuntu"), PLACEMENT, NETS
        )
        bus = lambda m: m["spec"]["template"]["spec"]["domain"]["devices"]["disks"][0]["disk"][
            "bus"
        ]
        assert bus(win) == "sata" and bus(lin) == "virtio"

    def test_the_pod_network_survives_alongside_range_networks(self):
        # The console path needs it; losing it is how a range deploys and nobody can reach it.
        m = virtual_machine_manifest(
            row(
                interfaces=[
                    InterfaceSpec(network="dmz", ip_address="172.30.10.5"),
                    InterfaceSpec(network="internal", ip_address="172.30.20.5"),
                ]
            ),
            PLACEMENT,
            NETS,
        )
        nets = m["spec"]["template"]["spec"]["networks"]
        assert nets[0] == {"name": "default", "pod": {}}
        assert [n["name"] for n in nets] == ["default", "net1", "net2"]

    def test_range_networks_are_bridged_not_masqueraded(self):
        m = virtual_machine_manifest(row(), PLACEMENT, NETS)
        ifaces = m["spec"]["template"]["spec"]["domain"]["devices"]["interfaces"]
        assert ifaces[0] == {"name": "default", "masquerade": {}}
        assert all("bridge" in i for i in ifaces[1:])

    def test_each_interface_references_its_own_attachment(self):
        # Not the network's shared NAD: KubeVirt overwrites the pod annotation that would have
        # carried the address, so the address has to be in the NAD the VM names.
        m = virtual_machine_manifest(row(), PLACEMENT, NETS)
        assert (
            m["spec"]["template"]["spec"]["networks"][1]["multus"]["networkName"]
            == "pg-alpha-jones/pg-net-dmz-" + m["metadata"]["name"]
        )

    def test_the_template_carries_no_multus_annotation(self):
        # KubeVirt generates it and would overwrite it; a manifest that sets one claims to do
        # something it does not.
        m = virtual_machine_manifest(row(), PLACEMENT, NETS)
        assert "k8s.v1.cni.cncf.io/networks" not in m["spec"]["template"]["metadata"].get(
            "annotations", {}
        )

    def test_an_unknown_network_is_refused_by_name(self):
        with pytest.raises(ValueError, match="unknown network 'wan'"):
            virtual_machine_manifest(
                row(interfaces=[InterfaceSpec(network="wan", ip_address="10.0.0.5")]),
                PLACEMENT,
                NETS,
            )

    def test_a_boot_image_becomes_a_registry_source(self):
        w = row(boot_image="registry.example/win2022@sha256:" + "f" * 64)
        dv = virtual_machine_manifest(w, PLACEMENT, NETS)["spec"]["dataVolumeTemplates"][0]
        assert "registry" in dv["spec"]["source"]

    def test_the_registry_url_carries_the_scheme_cdi_requires(self):
        # CDI wants `docker://<ref>`. The blueprint says `<ref>` -- a boot image is an OCI
        # reference, and a blueprint that has to know CDI's URL scheme knows which substrate
        # it is on.
        digest = "registry.example/win2022@sha256:" + "f" * 64
        dv = virtual_machine_manifest(row(boot_image=digest), PLACEMENT, NETS)
        url = dv["spec"]["dataVolumeTemplates"][0]["spec"]["source"]["registry"]["url"]
        assert url == f"docker://{digest}"

    def test_a_scheme_already_present_is_kept(self):
        given = "docker://registry.example/win2022@sha256:" + "f" * 64
        dv = virtual_machine_manifest(row(boot_image=given), PLACEMENT, NETS)
        assert dv["spec"]["dataVolumeTemplates"][0]["spec"]["source"]["registry"]["url"] == given

    def test_a_data_disk_is_blank_not_a_copy_of_the_boot_image(self):
        w = row(disk2_gb=50, boot_image="registry.example/win2022@sha256:" + "f" * 64)
        dvs = virtual_machine_manifest(w, PLACEMENT, NETS)["spec"]["dataVolumeTemplates"]
        assert "registry" in dvs[0]["spec"]["source"] and "blank" in dvs[1]["spec"]["source"]
