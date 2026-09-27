"""A typed workload spec -- COSMOS SUB-2.

`VM` carries roughly thirty flat provider columns: `windows_version`, `macos_version`,
`linux_distro`, `boot_mode`, `disk_type`, `iso_url`, `display_type`. Those are arguments to the
dockur and qemux container images. A vendor's API leaked into the domain model, and it is the
mistake CLAUDE.md architecture rule 4 names.

What replaces them is a spec that says what the learner needs -- an operating system, some CPUs,
some disks, some addresses on some networks -- and says nothing about who runs it. The adapter
below takes plain values rather than a `VM` row, for the same reason `placement_for_assignment`
does: `services/__init__` imports the Docker SDK, and the translation layer must not drag Era A
behind it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "DiskSpec",
    "InterfaceSpec",
    "OSFamily",
    "WorkloadSpec",
    "workload_from_vm_row",
]


class OSFamily(StrEnum):
    """What the guest is, not which image argument produces it."""

    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"


@dataclass(frozen=True, slots=True)
class DiskSpec:
    """A disk, in gigabytes, named by its role rather than its column."""

    name: str
    size_gb: int
    boot: bool = False

    def __post_init__(self) -> None:
        if self.size_gb <= 0:
            raise ValueError(f"disk {self.name!r} must be larger than zero")


@dataclass(frozen=True, slots=True)
class InterfaceSpec:
    """One attachment to one network.

    `network` is the range's own network name, not a CNI object name. Translating it is the
    networking layer's job; a workload declaring `br-a1b2c3` would be a workload that knows which
    substrate it is on.
    """

    network: str
    ip_address: str | None = None
    primary: bool = False

    def __post_init__(self) -> None:
        if not self.network:
            raise ValueError("an interface must name a network")


@dataclass(frozen=True, slots=True)
class WorkloadSpec:
    """What to run, expressed so that the thing running it is interchangeable."""

    name: str
    os_family: OSFamily
    os_version: str
    cpus: int
    memory_mb: int
    disks: tuple[DiskSpec, ...]
    interfaces: tuple[InterfaceSpec, ...]
    boot_image: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.cpus <= 0:
            raise ValueError(f"workload {self.name!r} needs at least one CPU")
        if self.memory_mb <= 0:
            raise ValueError(f"workload {self.name!r} needs memory")
        if not self.disks:
            raise ValueError(f"workload {self.name!r} needs at least one disk")
        if sum(1 for d in self.disks if d.boot) != 1:
            raise ValueError(f"workload {self.name!r} needs exactly one boot disk")
        if not self.interfaces:
            # A workload on no network is a workload nobody can reach, which is never what was
            # meant -- it is what a blueprint that forgot to attach one produces.
            raise ValueError(f"workload {self.name!r} needs at least one network interface")
        if sum(1 for i in self.interfaces if i.primary) > 1:
            raise ValueError(f"workload {self.name!r} has more than one primary interface")

    @property
    def primary_interface(self) -> InterfaceSpec:
        """The interface routing leaves by. The first attachment if none is marked."""
        return next((i for i in self.interfaces if i.primary), self.interfaces[0])


def workload_from_vm_row(
    *,
    hostname: str,
    cpu: int,
    ram_mb: int,
    disk_gb: int,
    windows_version: str | None = None,
    linux_distro: str | None = None,
    macos_version: str | None = None,
    disk2_gb: int | None = None,
    disk3_gb: int | None = None,
    boot_image: str | None = None,
    interfaces: Sequence[InterfaceSpec] = (),
) -> WorkloadSpec:
    """Collapse the flat provider columns into the spec.

    Exactly one of the three version columns may be set. A VM row with both `windows_version` and
    `linux_distro` is not a dual-boot machine, it is a bug -- and today nothing rejects it, which is
    how it survives into a deploy and produces whichever the first `if` happened to test.
    """
    declared = {
        OSFamily.WINDOWS: windows_version,
        OSFamily.LINUX: linux_distro,
        OSFamily.MACOS: macos_version,
    }
    present = {family: value for family, value in declared.items() if value}
    if not present:
        raise ValueError(
            f"vm {hostname!r} declares no operating system "
            "(windows_version, linux_distro or macos_version)"
        )
    if len(present) > 1:
        named = ", ".join(sorted(f.value for f in present))
        raise ValueError(f"vm {hostname!r} declares more than one operating system: {named}")

    family, version = next(iter(present.items()))
    disks = [DiskSpec(name="root", size_gb=disk_gb, boot=True)]
    for index, extra in enumerate((disk2_gb, disk3_gb), start=2):
        if extra:
            disks.append(DiskSpec(name=f"data{index - 1}", size_gb=extra))

    return WorkloadSpec(
        name=hostname,
        os_family=family,
        os_version=str(version),
        cpus=cpu,
        memory_mb=ram_mb,
        disks=tuple(disks),
        interfaces=tuple(interfaces),
        boot_image=boot_image,
        labels={"pg.workload/os": family.value},
    )
