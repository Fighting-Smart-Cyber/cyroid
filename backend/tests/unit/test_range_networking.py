"""Range networks on CNI -- COSMOS PG-58."""

import json

import pytest

from proving_ground.capability.networking import (
    MULTUS_ANNOTATION,
    NetworkSpec,
    attachment_annotation,
    interface_attachment_definition,
    network_attachment_definition,
)
from proving_ground.capability.workload import InterfaceSpec, OSFamily, DiskSpec, WorkloadSpec

NETS = [
    NetworkSpec(name="dmz", subnet="172.30.10.0/24", gateway="172.30.10.1"),
    NetworkSpec(name="internal", subnet="172.30.20.0/24", gateway="172.30.20.1"),
    NetworkSpec(name="mgmt", subnet="172.30.30.0/24"),
]


def workload(*interfaces):
    return WorkloadSpec(
        name="multihomed",
        os_family=OSFamily.LINUX,
        os_version="ubuntu",
        cpus=1,
        memory_mb=512,
        disks=(DiskSpec("root", 10, boot=True),),
        interfaces=tuple(interfaces),
    )


class TestSubnetsAreValidated:
    def test_an_invalid_subnet_is_refused(self):
        with pytest.raises(ValueError, match="invalid subnet"):
            NetworkSpec(name="bad", subnet="not-a-cidr")

    @pytest.mark.parametrize("subnet", ["10.42.5.0/24", "10.43.0.0/16", "10.42.0.0/16"])
    def test_overlapping_the_cluster_ranges_is_refused(self, subnet):
        # Range traffic colliding with pod or service traffic is intermittent and hard to attribute.
        with pytest.raises(ValueError, match="overlaps the cluster"):
            NetworkSpec(name="clash", subnet=subnet)

    def test_a_gateway_outside_its_subnet_is_refused(self):
        with pytest.raises(ValueError, match="outside"):
            NetworkSpec(name="dmz", subnet="172.30.10.0/24", gateway="172.30.99.1")


class TestAttachmentDefinitions:
    def test_three_networks_give_three_distinct_attachments(self):
        names = {network_attachment_definition(n, "ns")["metadata"]["name"] for n in NETS}
        assert len(names) == 3

    def test_it_is_a_bridge_isolated_from_the_host(self):
        config = json.loads(network_attachment_definition(NETS[0], "ns")["spec"]["config"])
        # macvlan would put the range on the node's physical network -- the opposite of a range.
        assert config["type"] == "bridge" and config["isolateFromHost"] is True

    def test_ipam_is_static_so_addresses_match_the_blueprint(self):
        config = json.loads(network_attachment_definition(NETS[0], "ns")["spec"]["config"])
        assert config["ipam"]["type"] == "static"

    def test_isgateway_follows_whether_one_was_declared(self):
        with_gw = json.loads(network_attachment_definition(NETS[0], "ns")["spec"]["config"])
        without = json.loads(network_attachment_definition(NETS[2], "ns")["spec"]["config"])
        assert with_gw["isGateway"] is True and without["isGateway"] is False


class TestMultiHomedAttachment:
    def test_a_workload_on_three_networks_gets_three_attachments(self):
        wl = workload(
            InterfaceSpec(network="dmz", ip_address="172.30.10.5", primary=True),
            InterfaceSpec(network="internal", ip_address="172.30.20.5"),
            InterfaceSpec(network="mgmt", ip_address="172.30.30.5"),
        )
        entries = json.loads(attachment_annotation(wl, NETS, "ns")[MULTUS_ANNOTATION])
        assert len(entries) == 3
        assert [e["interface"] for e in entries] == ["net1", "net2", "net3"]

    def test_each_address_carries_its_own_prefix(self):
        wl = workload(
            InterfaceSpec(network="dmz", ip_address="172.30.10.5"),
            InterfaceSpec(network="internal", ip_address="172.30.20.5"),
        )
        entries = json.loads(attachment_annotation(wl, NETS, "ns")[MULTUS_ANNOTATION])
        assert entries[0]["ips"] == ["172.30.10.5/24"]
        assert entries[1]["ips"] == ["172.30.20.5/24"]

    def test_a_gateway_is_passed_only_where_declared(self):
        wl = workload(
            InterfaceSpec(network="dmz", ip_address="172.30.10.5"),
            InterfaceSpec(network="mgmt", ip_address="172.30.30.5"),
        )
        entries = json.loads(attachment_annotation(wl, NETS, "ns")[MULTUS_ANNOTATION])
        assert entries[0]["gateway"] == ["172.30.10.1"]
        assert "gateway" not in entries[1]

    def test_an_interface_with_no_address_is_refused(self):
        # Static IPAM cannot invent one, and a silently address-less second interface looks
        # deployed while half the exercise does not work.
        with pytest.raises(ValueError, match="no address on"):
            attachment_annotation(workload(InterfaceSpec(network="dmz")), NETS, "ns")

    def test_an_unknown_network_names_what_the_range_declares(self):
        with pytest.raises(ValueError, match="dmz, internal, mgmt"):
            attachment_annotation(
                workload(InterfaceSpec(network="wan", ip_address="10.0.0.5")), NETS, "ns"
            )


class TestPerInterfaceAttachments:
    """KubeVirt builds the pod's Multus annotation itself, from `spec.networks`, and overwrites
    any the VM template carried -- so `ips` in an annotation never reach static IPAM, and the
    launcher pod fails at sandbox creation with `IPAM plugin returned missing IP config`
    (observed on pg-devtest, 2026-09-15). The address therefore lives in a
    NetworkAttachmentDefinition of its own, one per workload interface, on the same bridge."""

    def test_the_attachment_is_named_for_the_network_and_the_workload(self):
        nad = interface_attachment_definition(
            workload(InterfaceSpec(network="dmz", ip_address="172.30.10.5")),
            InterfaceSpec(network="dmz", ip_address="172.30.10.5"),
            NETS[0],
            "ns",
        )
        assert nad["metadata"]["name"] == "pg-net-dmz-multihomed"
        assert nad["metadata"]["namespace"] == "ns"

    def test_it_shares_the_networks_bridge(self):
        # Two workloads on 'dmz' must land on one L2 segment, not one bridge each.
        iface = InterfaceSpec(network="dmz", ip_address="172.30.10.5")
        shared = json.loads(network_attachment_definition(NETS[0], "ns")["spec"]["config"])
        own = json.loads(
            interface_attachment_definition(workload(iface), iface, NETS[0], "ns")["spec"]["config"]
        )
        assert own["bridge"] == shared["bridge"]
        assert own["type"] == "bridge" and own["isolateFromHost"] is True

    def test_the_address_and_gateway_are_in_the_ipam_config(self):
        iface = InterfaceSpec(network="dmz", ip_address="172.30.10.5")
        config = json.loads(
            interface_attachment_definition(workload(iface), iface, NETS[0], "ns")["spec"]["config"]
        )
        assert config["ipam"] == {
            "type": "static",
            "addresses": [{"address": "172.30.10.5/24", "gateway": "172.30.10.1"}],
        }

    def test_no_gateway_means_no_gateway(self):
        iface = InterfaceSpec(network="mgmt", ip_address="172.30.30.5")
        config = json.loads(
            interface_attachment_definition(workload(iface), iface, NETS[2], "ns")["spec"]["config"]
        )
        assert config["ipam"]["addresses"] == [{"address": "172.30.30.5/24"}]

    def test_an_interface_without_an_address_is_refused(self):
        iface = InterfaceSpec(network="dmz")
        with pytest.raises(ValueError, match="no address"):
            interface_attachment_definition(workload(iface), iface, NETS[0], "ns")

    def test_an_address_outside_the_network_is_refused(self):
        # A blueprint typo that would otherwise deploy a host unreachable on the network it
        # claims to be on.
        iface = InterfaceSpec(network="dmz", ip_address="172.30.20.5")
        with pytest.raises(ValueError, match="outside"):
            interface_attachment_definition(workload(iface), iface, NETS[0], "ns")

    def test_the_attachment_carries_the_labels_teardown_and_evidence_read(self):
        iface = InterfaceSpec(network="dmz", ip_address="172.30.10.5")
        nad = interface_attachment_definition(workload(iface), iface, NETS[0], "ns")
        assert nad["metadata"]["labels"] == {
            "pg.network/name": "dmz",
            "pg.workload/name": "multihomed",
        }


class TestTheReservedRangesAreTheClustersNotK3sS:
    """The guard hardcoded k3s's 10.42/16 and 10.43/16.

    That is wrong in both directions anywhere else: it rejects a perfectly good subnet on a
    cluster that does not use those ranges, and it waves through a subnet that genuinely
    collides with the cluster it IS running on. AKS kubenet defaults to 10.244.0.0/16 pods and
    10.0.0.0/16 services; on Azure CNI pod addresses come out of the VNet.

    The engine pushes the values in -- this package is the licence boundary and imports nothing
    from the rest of the engine, so it cannot read settings for itself.
    """

    @pytest.fixture(autouse=True)
    def _restore(self):
        from proving_ground.capability import networking as net

        before = net.reserved_cidrs()
        yield
        net.set_reserved_cidrs([str(c) for c in before])

    def test_the_k3s_defaults_still_apply_when_nothing_configures_them(self):
        with pytest.raises(ValueError, match="overlaps"):
            NetworkSpec(name="clash", subnet="10.42.0.0/16")

    def test_a_cluster_can_declare_its_own(self):
        from proving_ground.capability import networking as net

        net.set_reserved_cidrs("10.244.0.0/16,10.0.0.0/16")
        # k3s's ranges are not reserved on this cluster, so this is legitimate.
        NetworkSpec(name="fine", subnet="10.42.0.0/24")
        # ...and AKS kubenet's pod range now is.
        with pytest.raises(ValueError, match="overlaps"):
            NetworkSpec(name="clash", subnet="10.244.5.0/24")

    def test_an_unparseable_entry_is_dropped_rather_than_failing_every_deploy(self):
        from proving_ground.capability import networking as net

        net.set_reserved_cidrs("not-a-cidr, 10.0.0.0/16")
        with pytest.raises(ValueError, match="overlaps"):
            NetworkSpec(name="clash", subnet="10.0.1.0/24")
        NetworkSpec(name="fine", subnet="192.168.5.0/24")
