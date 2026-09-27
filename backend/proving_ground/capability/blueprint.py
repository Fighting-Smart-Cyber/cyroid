"""Read a range blueprint, whichever era wrote it -- COSMOS PG-122.

`RangeBlueprint.config` is free-form JSON. Today it describes an Era A range: networks, VMs, MSEL
and router, shaped by what the DinD path needed. It has to also describe an Era B range --
networks, workloads, capability packages -- without breaking the catalog blueprints that already
exist, because `cyroid-catalog` resolves against this repository and that integration is the thing
this repository exists to validate.

So the config is **versioned**, and the version is explicit rather than sniffed. A blueprint with
no `schemaVersion` is v1 by definition: that is what every blueprint written before today is, and
guessing from shape would mean a v2 blueprint that happens to omit `workloads` is silently read as
Era A.

A v1 blueprint parses. It does not raise, it does not half-translate, and it is marked `legacy` so
a caller can say "this is an Era A blueprint" in its own words. Translating v1 into v2 is
PG-79 / MIG-2 and deliberately not attempted here -- a half-translation that looks like a k8s range
is worse than an honest refusal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .models import Scope
from .networking import NetworkSpec
from .runtime import CapabilitySpec
from .specs import capabilities_from_blueprint_config
from .workload import DiskSpec, InterfaceSpec, OSFamily, WorkloadSpec

__all__ = [
    "SCHEMA_VERSION_K8S",
    "SCHEMA_VERSION_LEGACY",
    "RangeSpec",
    "read_blueprint",
]

SCHEMA_VERSION_LEGACY = 1
SCHEMA_VERSION_K8S = 2
_SUPPORTED = (SCHEMA_VERSION_LEGACY, SCHEMA_VERSION_K8S)


@dataclass(frozen=True, slots=True)
class RangeSpec:
    """What a blueprint describes, in substrate-neutral terms."""

    schema_version: int
    networks: tuple[NetworkSpec, ...] = ()
    workloads: tuple[WorkloadSpec, ...] = ()
    capabilities: tuple[CapabilitySpec, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_legacy(self) -> bool:
        return self.schema_version == SCHEMA_VERSION_LEGACY

    @property
    def deployable_on_kubernetes(self) -> bool:
        """A v1 blueprint is not deployable on the k8s substrate until PG-79 migrates it.

        Answered as a property rather than left to each caller to infer, because "no workloads"
        and "Era A blueprint" look identical from the outside and mean very different things.
        """
        return not self.is_legacy


def read_blueprint(config: Mapping[str, Any] | None) -> RangeSpec:
    """Parse a blueprint config into a `RangeSpec`."""
    config = config or {}
    if not isinstance(config, Mapping):
        raise ValueError("blueprint config must be an object")

    version = config.get("schemaVersion", SCHEMA_VERSION_LEGACY)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError(f"blueprint schemaVersion must be an integer, got {version!r}")
    if version not in _SUPPORTED:
        supported = ", ".join(str(v) for v in _SUPPORTED)
        raise ValueError(
            f"blueprint schemaVersion {version} is not supported (known: {supported}). "
            "A newer blueprint than this engine understands is a refusal, not a best effort -- "
            "silently ignoring fields it declares would deploy a range missing whatever they said."
        )

    if version == SCHEMA_VERSION_LEGACY:
        # Parsed, not translated. Capability packages are read even from a v1 blueprint because
        # they are era-neutral -- a capability does not care which substrate runs it.
        return RangeSpec(
            schema_version=version,
            capabilities=tuple(capabilities_from_blueprint_config(config)),
            raw=config,
        )

    return RangeSpec(
        schema_version=version,
        networks=tuple(_networks(config)),
        workloads=tuple(_workloads(config)),
        capabilities=tuple(capabilities_from_blueprint_config(config)),
        raw=config,
    )


def _networks(config: Mapping[str, Any]) -> list[NetworkSpec]:
    declared = _sequence(config.get("networks"), "networks")
    networks = []
    for index, entry in enumerate(declared):
        where = f"networks[{index}]"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{where} must be an object")
        networks.append(
            NetworkSpec(
                name=str(_require(entry, "name", where)),
                subnet=str(_require(entry, "subnet", where)),
                gateway=str(entry["gateway"]) if entry.get("gateway") else None,
            )
        )
    names = [n.name for n in networks]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        # Two networks sharing a name means an interface referring to it is ambiguous, and the
        # one that wins is whichever the dict comprehension saw last.
        raise ValueError(
            f"blueprint declares duplicate network names: {', '.join(sorted(duplicates))}"
        )
    return networks


def _workloads(config: Mapping[str, Any]) -> list[WorkloadSpec]:
    declared = _sequence(config.get("workloads"), "workloads")
    return [_workload(entry, index) for index, entry in enumerate(declared)]


def _workload(entry: Any, index: int) -> WorkloadSpec:
    where = f"workloads[{index}]"
    if not isinstance(entry, Mapping):
        raise ValueError(f"{where} must be an object")
    name = str(_require(entry, "name", where))
    where = f"workload {name!r}"

    os_block = _require(entry, "os", where)
    if not isinstance(os_block, Mapping):
        raise ValueError(f"{where} 'os' must be an object with 'family' and 'version'")
    raw_family = _require(os_block, "family", f"{where} os")
    try:
        family = OSFamily(raw_family)
    except ValueError:
        allowed = ", ".join(f.value for f in OSFamily)
        raise ValueError(
            f"{where} has os.family {raw_family!r}; must be one of {allowed}"
        ) from None

    disks = [
        DiskSpec(
            name=str(_require(d, "name", f"{where} disk")),
            size_gb=int(_require(d, "sizeGb", f"{where} disk")),
            boot=bool(d.get("boot", False)),
        )
        for d in _sequence(entry.get("disks"), f"{where} disks")
        if isinstance(d, Mapping) or _raise(f"{where} each disk must be an object")
    ]

    interfaces = [
        InterfaceSpec(
            network=str(_require(i, "network", f"{where} interface")),
            ip_address=str(i["ip"]) if i.get("ip") else None,
            primary=bool(i.get("primary", False)),
        )
        for i in _sequence(entry.get("interfaces"), f"{where} interfaces")
        if isinstance(i, Mapping) or _raise(f"{where} each interface must be an object")
    ]

    return WorkloadSpec(
        name=name,
        os_family=family,
        os_version=str(_require(os_block, "version", f"{where} os")),
        cpus=int(_require(entry, "cpus", where)),
        memory_mb=int(_require(entry, "memoryMb", where)),
        disks=tuple(disks),
        interfaces=tuple(interfaces),
        boot_image=str(entry["bootImage"]) if entry.get("bootImage") else None,
    )


def _sequence(value: Any, where: str) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"blueprint '{where}' must be a list")
    return value


def _require(source: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in source or source[key] in (None, ""):
        raise ValueError(f"{where} is missing required field {key!r}")
    return source[key]


def _raise(message: str) -> bool:
    raise ValueError(message)


def scopes_of(spec: RangeSpec) -> frozenset[Scope]:
    """Every capability scope the blueprint declares -- what placement needs to decide."""
    return frozenset(c.scope for c in spec.capabilities)
