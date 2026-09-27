"""Translate a `WorkloadSpec` into a KubeVirt `VirtualMachine` -- COSMOS PG-42.

KubeVirt rather than a container running nested QEMU. The range's workloads are virtual machines;
expressing them as `dockur/windows` pods would carry the vendor's argument surface onto the new
substrate, which is the thing PG-42 exists to remove. `pg-devtest` runs KubeVirt on real KVM, so the
guest gets hardware virtualisation instead of two layers of emulation.

Nothing here knows about ranges, learners or placement. It takes a spec and returns a manifest.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .models import Placement, to_dns_label
from .networking import NetworkSpec, interface_attachment_name
from .workload import OSFamily, WorkloadSpec

__all__ = ["KUBEVIRT_API", "virtual_machine_manifest"]

KUBEVIRT_API = "kubevirt.io/v1"

# Windows guests need a SATA bus and a different machine type than a Linux guest is happiest with.
# This is the one place OS family is allowed to change the shape of the output -- and it is a
# property of the guest, not an argument copied from a vendor's container image.
_BUS = {OSFamily.WINDOWS: "sata", OSFamily.LINUX: "virtio", OSFamily.MACOS: "sata"}


def _registry_url(image: str) -> str:
    """CDI imports from `docker://<ref>`; a blueprint declares `<ref>`.

    The scheme is CDI's, so it is added here rather than expected of the blueprint -- a blueprint
    that has to spell `docker://` knows which substrate it is on.
    """
    return image if "://" in image else f"docker://{image}"


def virtual_machine_manifest(
    workload: WorkloadSpec,
    placement: Placement,
    networks: Sequence[NetworkSpec] = (),
    *,
    running: bool = True,
    extra_labels: Mapping[str, str] | None = None,
) -> dict:
    """Build the `VirtualMachine` object for one workload."""
    name = to_dns_label(workload.name)
    labels = {
        "pg.workload/name": name,
        "pg.workload/os": workload.os_family.value,
        **dict(workload.labels),
        **dict(extra_labels or {}),
    }

    disks, volumes = [], []
    for disk in workload.disks:
        disk_name = to_dns_label(disk.name)
        entry = {"name": disk_name, "disk": {"bus": _BUS[workload.os_family]}}
        if disk.boot:
            # Lower number boots first. Only the boot disk gets an order, so an added data disk
            # cannot silently become the boot device.
            entry["bootOrder"] = 1
        disks.append(entry)
        volumes.append({"name": disk_name, "dataVolume": {"name": f"{name}-{disk_name}"}})

    data_volume_templates = [
        {
            "metadata": {"name": f"{name}-{to_dns_label(disk.name)}"},
            "spec": {
                "storage": {
                    "resources": {"requests": {"storage": f"{disk.size_gb}Gi"}},
                    "accessModes": ["ReadWriteOnce"],
                },
                "source": (
                    {"registry": {"url": _registry_url(workload.boot_image)}}
                    if disk.boot and workload.boot_image
                    else {"blank": {}}
                ),
            },
        }
        for disk in workload.disks
    ]

    # The pod network stays: the console path and cluster traffic both need it. Range networks are
    # additional interfaces, which is why `masquerade` is used for the default and `bridge` for the
    # rest -- masquerading a range network would hide the address the exercise tells the learner to
    # expect. Each interface names its *own* attachment definition, the one carrying its address:
    # KubeVirt writes the pod's Multus annotation itself and would overwrite one set here.
    vm_networks = [{"name": "default", "pod": {}}]
    interfaces = [{"name": "default", "masquerade": {}}]
    by_name = {n.name: n for n in networks}
    for index, attachment in enumerate(workload.interfaces, start=1):
        network = by_name.get(attachment.network)
        if network is None:
            known = ", ".join(sorted(by_name)) or "none"
            raise ValueError(
                f"workload {workload.name!r} attaches to unknown network "
                f"{attachment.network!r}; the range declares: {known}"
            )
        iface = f"net{index}"
        attachment = interface_attachment_name(network, workload)
        vm_networks.append(
            {"name": iface, "multus": {"networkName": f"{placement.namespace}/{attachment}"}}
        )
        interfaces.append({"name": iface, "bridge": {}})

    return {
        "apiVersion": KUBEVIRT_API,
        "kind": "VirtualMachine",
        "metadata": {"name": name, "namespace": placement.namespace, "labels": labels},
        "spec": {
            "running": running,
            "dataVolumeTemplates": data_volume_templates,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "domain": {
                        "cpu": {"cores": workload.cpus},
                        "memory": {"guest": f"{workload.memory_mb}Mi"},
                        "devices": {"disks": disks, "interfaces": interfaces},
                    },
                    "networks": vm_networks,
                    "volumes": volumes,
                },
            },
        },
    }
