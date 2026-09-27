"""Range networks on CNI -- COSMOS RNET-1 (#58).

Ranges are multi-network with multi-homed hosts, and that is the product, not a detail. On Docker
it came from user-defined bridges. On Kubernetes it comes from Multus: each range network becomes a
`NetworkAttachmentDefinition`, and a workload attaches to several by annotation.

The pod's default interface stays what the CNI gives it -- cluster traffic and the console path
need it. Range networks are *additional* interfaces, which is also why a range network claiming the
pod network's subnet is rejected rather than merged.

Two kinds of attachment definition, because two kinds of thing attach. A **pod** attaches by
annotation, which can carry its addresses (`attachment_annotation`). A KubeVirt **VM** cannot:
virt-controller generates the pod's Multus annotation from `spec.networks` and overwrites whatever
the template carried, so `ips` written there never reach static IPAM and the launcher pod dies at
sandbox creation with `IPAM plugin returned missing IP config` (pg-devtest, 2026-09-15). A VM's
address therefore lives in an attachment definition of its own -- one per workload interface, on
the network's bridge (`interface_attachment_definition`).
"""

from __future__ import annotations

import ipaddress
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from .models import to_dns_label
from .workload import InterfaceSpec, WorkloadSpec

logger = logging.getLogger(__name__)

__all__ = [
    "MULTUS_ANNOTATION",
    "reserved_cidrs",
    "set_reserved_cidrs",
    "NetworkSpec",
    "attachment_annotation",
    "interface_attachment_definition",
    "interface_attachment_name",
    "network_attachment_definition",
]

MULTUS_ANNOTATION = "k8s.v1.cni.cncf.io/networks"


# The cluster's own pod and service ranges. A range network overlapping these does not produce
# an isolated network -- it produces intermittent, hard-to-attribute breakage in whichever of the
# two the kernel happens to route first.
#
# These were hardcoded to k3s's 10.42/16 and 10.43/16, which made the guard wrong in both
# directions anywhere else: it rejects a legitimate subnet on a cluster that does not use those,
# and it waves through one that genuinely collides with the cluster it IS running on. AKS kubenet
# defaults to 10.244.0.0/16 pods and 10.0.0.0/16 services; on Azure CNI pod addresses come out of
# the VNet.
#
# The engine pushes its cluster's values in at startup rather than the contract reading settings:
# this package is the licence boundary (ADR-0011) and imports nothing from the rest of the engine.
_DEFAULT_RESERVED = ("10.42.0.0/16", "10.43.0.0/16")
_RESERVED: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(c) for c in _DEFAULT_RESERVED
)


def reserved_cidrs() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """The ranges a range network may not overlap."""
    return _RESERVED


def set_reserved_cidrs(cidrs: Sequence[str] | str) -> None:
    """Tell the contract what this cluster uses. Unparseable entries are dropped, not fatal.

    A typo in a cluster-wide setting should not make every deploy fail; it should narrow the
    guard and say so, which is the same trade the rest of this module makes.
    """
    global _RESERVED
    raw = cidrs.split(",") if isinstance(cidrs, str) else list(cidrs)
    parsed = []
    for entry in raw:
        entry = entry.strip()
        if not entry:
            continue
        try:
            parsed.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("ignoring unparseable reserved CIDR %r", entry)
    _RESERVED = tuple(parsed)


@dataclass(frozen=True, slots=True)
class NetworkSpec:
    """One range network, as declared by the range rather than by the CNI."""

    name: str
    subnet: str
    gateway: str | None = None

    def __post_init__(self) -> None:
        try:
            network = ipaddress.ip_network(self.subnet, strict=False)
        except ValueError as exc:
            raise ValueError(f"network {self.name!r} has an invalid subnet: {exc}") from exc
        for reserved in reserved_cidrs():
            if network.overlaps(reserved):
                raise ValueError(
                    f"network {self.name!r} subnet {self.subnet} overlaps the cluster's "
                    f"{reserved} range; range traffic and cluster traffic would collide"
                )
        if self.gateway:
            try:
                address = ipaddress.ip_address(self.gateway)
            except ValueError as exc:
                raise ValueError(f"network {self.name!r} has an invalid gateway: {exc}") from exc
            if address not in network:
                raise ValueError(
                    f"network {self.name!r} gateway {self.gateway} is outside {self.subnet}"
                )

    @property
    def attachment_name(self) -> str:
        return to_dns_label("pg", "net", self.name)

    @property
    def bridge(self) -> str:
        """The Linux bridge every attachment to this network shares. 15 chars: IFNAMSIZ."""
        return to_dns_label("br", self.name, limit=15)


def network_attachment_definition(spec: NetworkSpec, namespace: str) -> dict:
    """A Multus `NetworkAttachmentDefinition` for one range network.

    `bridge` with `isolateFromHost` rather than `macvlan`: a range network is meant to be a closed
    L2 segment between its workloads, and macvlan would put it on the node's physical network, which
    is the opposite of what a range is for.

    IPAM is `static` -- addresses come from the VM rows, which is where the blueprint already put
    them. Handing the range a DHCP server would mean the addresses a learner sees differ from the
    ones the blueprint and the exercise text say.
    """
    config = {
        "cniVersion": "0.3.1",
        "name": spec.attachment_name,
        "type": "bridge",
        "bridge": spec.bridge,
        "isGateway": bool(spec.gateway),
        "isolateFromHost": True,
        "ipam": {"type": "static"},
    }
    return {
        "apiVersion": "k8s.cni.cncf.io/v1",
        "kind": "NetworkAttachmentDefinition",
        "metadata": {
            "name": spec.attachment_name,
            "namespace": namespace,
            "labels": {"pg.network/name": to_dns_label(spec.name)},
        },
        "spec": {"config": json.dumps(config)},
    }


def interface_attachment_name(network: NetworkSpec, workload: WorkloadSpec) -> str:
    return to_dns_label(network.attachment_name, workload.name)


def interface_attachment_definition(
    workload: WorkloadSpec, interface: InterfaceSpec, network: NetworkSpec, namespace: str
) -> dict:
    """One workload's attachment to one range network, address included.

    Same bridge as the network's own definition, so every workload on 'dmz' shares one L2
    segment; its own IPAM block, so the address the blueprint declared is the one the CNI hands
    the launcher pod -- and, through KubeVirt's bridge binding, the one the guest gets.
    """
    if interface.network != network.name:
        raise ValueError(
            f"interface on {interface.network!r} cannot attach to network {network.name!r}"
        )
    if not interface.ip_address:
        raise ValueError(
            f"workload {workload.name!r} has no address on {network.name!r}; "
            "static IPAM cannot assign one"
        )
    subnet = ipaddress.ip_network(network.subnet, strict=False)
    if ipaddress.ip_address(interface.ip_address) not in subnet:
        raise ValueError(
            f"workload {workload.name!r} address {interface.ip_address} is outside "
            f"{network.name!r} ({network.subnet})"
        )
    address: dict[str, str] = {"address": f"{interface.ip_address}/{subnet.prefixlen}"}
    if network.gateway:
        address["gateway"] = network.gateway
    config = {
        "cniVersion": "0.3.1",
        "name": interface_attachment_name(network, workload),
        "type": "bridge",
        "bridge": network.bridge,
        "isGateway": bool(network.gateway),
        "isolateFromHost": True,
        "ipam": {"type": "static", "addresses": [address]},
    }
    return {
        "apiVersion": "k8s.cni.cncf.io/v1",
        "kind": "NetworkAttachmentDefinition",
        "metadata": {
            "name": interface_attachment_name(network, workload),
            "namespace": namespace,
            "labels": {
                "pg.network/name": to_dns_label(network.name),
                "pg.workload/name": to_dns_label(workload.name),
            },
        },
        "spec": {"config": json.dumps(config)},
    }


def attachment_annotation(
    workload: WorkloadSpec, networks: Sequence[NetworkSpec], namespace: str
) -> dict[str, str]:
    """The Multus annotation attaching a workload to each of its range networks.

    Every interface carries its address explicitly. A multi-homed host whose second interface comes
    up without one is the failure this exists to prevent: it looks deployed, and the half of the
    exercise that crosses that network simply does not work.
    """
    by_name = {n.name: n for n in networks}
    attachments = []
    for interface in workload.interfaces:
        network = by_name.get(interface.network)
        if network is None:
            known = ", ".join(sorted(by_name)) or "none"
            raise ValueError(
                f"workload {workload.name!r} attaches to unknown network "
                f"{interface.network!r}; the range declares: {known}"
            )
        if not interface.ip_address:
            raise ValueError(
                f"workload {workload.name!r} has no address on {interface.network!r}; "
                "static IPAM cannot assign one"
            )
        prefix = ipaddress.ip_network(network.subnet, strict=False).prefixlen
        entry = {
            "name": network.attachment_name,
            "namespace": namespace,
            "interface": f"net{len(attachments) + 1}",
            "ips": [f"{interface.ip_address}/{prefix}"],
        }
        if network.gateway:
            entry["gateway"] = [network.gateway]
        attachments.append(entry)

    return {MULTUS_ANNOTATION: json.dumps(attachments)}
