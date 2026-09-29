# backend/proving_ground/schemas/blueprint.py
from typing import Any, Optional, List
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field, model_validator

# ============ Config Sub-schemas ============


class NetworkConfig(BaseModel):
    name: str
    subnet: str
    gateway: str
    is_isolated: bool = False
    internet_enabled: bool = False
    dhcp_enabled: bool = False


class NetworkInterfaceConfig(BaseModel):
    """Network interface configuration for multi-NIC VMs in blueprints."""

    network_name: str
    ip_address: Optional[str] = None  # None = auto-assign on import
    is_primary: bool = False


class VMConfig(BaseModel):
    hostname: str
    # Legacy fields (kept for backward compatibility with older blueprints)
    ip_address: Optional[str] = None
    network_name: Optional[str] = None
    # Multi-NIC support: list of network interfaces
    # If present, takes precedence over legacy ip_address/network_name
    network_interfaces: Optional[List[NetworkInterfaceConfig]] = None
    # Image Library sources - exactly one required
    base_image_id: Optional[str] = None
    golden_image_id: Optional[str] = None
    snapshot_id: Optional[str] = None
    # Fallback fields for cross-environment portability (Issue #80)
    base_image_name: Optional[str] = None
    base_image_tag: Optional[str] = None
    # Deprecated: kept for backward compatibility with older blueprints
    template_name: Optional[str] = None
    cpu: int = 1
    ram_mb: int = 1024
    disk_gb: int = 20
    position_x: Optional[int] = None
    position_y: Optional[int] = None
    # Windows version code for dockur/windows VMs (e.g., "11", "10", "2022")
    windows_version: Optional[str] = None
    # Target architecture override (e.g., "x86_64" or "arm64")
    # When set, Docker pulls the specific platform variant instead of host default
    arch: Optional[str] = None
    # Environment variables passed to the container at runtime
    environment: Optional[dict[str, str]] = None


class RouterConfig(BaseModel):
    enabled: bool = True
    dhcp_enabled: bool = False


class MSELConfig(BaseModel):
    content: Optional[str] = None
    format: str = "yaml"
    walkthrough: Optional[dict] = None  # Structured walkthrough/guide content


class BlueprintConfig(BaseModel):
    """An Era A blueprint config, and only that.

    A v2 config describes workloads and capability packages instead of VMs, and is read by
    `capability.blueprint.read_blueprint`. This model does not know those keys: it refuses a v2
    config, which has no `vms`, and would silently drop the workloads and capabilities of
    anything it did accept. So the API keeps config free-form and reaches for this model only
    once it knows the config is v1.
    """

    networks: List[NetworkConfig]
    vms: List[VMConfig]
    router: Optional[RouterConfig] = None
    msel: Optional[MSELConfig] = None
    content_ids: Optional[List[str]] = None  # Linked content IDs for static reference


# ============ Blueprint Schemas ============


class BlueprintCreate(BaseModel):
    """A blueprint is either extracted from an existing range or supplied as a config.

    Extraction reads Network and VM rows, so it can only ever describe an Era A range. A
    Kubernetes (v2) blueprint has no rows to read -- its workloads and capability packages are
    realised on the cluster at deploy time and nowhere else -- so `config` is the only way to
    author one.
    """

    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    range_id: Optional[UUID] = None
    # Free-form on purpose: the era the config declares decides how it is read, and the API
    # validates it with `capability.blueprint.read_blueprint` rather than with the v1 model above.
    config: Optional[dict[str, Any]] = None

    @model_validator(mode="after")
    def _one_source(self) -> "BlueprintCreate":
        if (self.range_id is None) == (self.config is None):
            raise ValueError("provide exactly one of 'range_id' or 'config'")
        return self


class BlueprintUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    content_ids: Optional[List[str]] = None  # Linked content for training events
    # Direct config update (increments version). Free-form for the same reason as
    # BlueprintCreate.config: typed as BlueprintConfig, a PUT carrying a v2 config is either
    # refused outright or -- if it happens to satisfy the v1 fields -- accepted with its workloads
    # and capabilities dropped on the way to the database.
    config: Optional[dict[str, Any]] = None


class BlueprintResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str]
    version: int
    content_ids: List[str] = []  # Linked content for training events
    created_by: Optional[UUID] = None  # Nullable for seed blueprints
    created_at: datetime
    updated_at: datetime
    network_count: int = 0
    # The machine count, whichever era wrote the config: a v2 blueprint's machines are its
    # workloads. The name is the v1 one because the field is part of the published client.
    vm_count: int = 0
    # Which era this blueprint is written in: 1 for networks and VMs, 2 for workloads and
    # capabilities. Without it a card can only label its count per install, so a v1 blueprint
    # sitting in a Kubernetes install's catalog reads as machines it cannot deploy.
    schema_version: int = 1
    instance_count: int = 0
    is_seed: bool = False  # True for built-in blueprints

    class Config:
        from_attributes = True


class BlueprintDetailResponse(BlueprintResponse):
    # The stored document, as written. Forcing it through BlueprintConfig 500s on every v2
    # blueprint, which has no `vms`; a discriminated union on schemaVersion cannot replace it
    # either, because a v1 config carries no schemaVersion at all -- absence is what makes it v1 --
    # and pydantic cannot tag a union on a key that is not there.
    config: dict[str, Any]
    created_by_username: Optional[str] = None


# ============ Instance Schemas ============


class InstanceDeploy(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    auto_deploy: bool = True


class InstanceResponse(BaseModel):
    id: UUID
    name: str
    blueprint_id: UUID
    blueprint_version: int
    subnet_offset: int
    instructor_id: UUID
    range_id: UUID
    created_at: datetime
    # Denormalized fields for convenience
    range_name: Optional[str] = None
    range_status: Optional[str] = None
    instructor_username: Optional[str] = None

    class Config:
        from_attributes = True
