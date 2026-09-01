# backend/proving_ground/schemas/vm.py
from datetime import datetime
from typing import Optional, Literal, List, Dict
from uuid import UUID
from pydantic import BaseModel, Field, computed_field, field_serializer, model_validator

from proving_ground.models.vm import VMStatus

# Host devices a VM may opt into, keyed by a short stable name. The KEY is what
# crosses the API and what is stored on the VM; the path is resolved from this
# table server-side. Mapping a device is host-level access handed into a
# container a student can reach, so the set is fixed here rather than accepting
# a path from the client - otherwise "devices" becomes a way to hand out
# /dev/sda. Adding an entry is a deliberate code change, reviewable as such.
ALLOWED_DEVICES: Dict[str, Dict[str, str]] = {
    "tun": {
        "path": "/dev/net/tun:/dev/net/tun:rwm",
        "label": "VPN / tunnel support",
        "help": "Required by any VPN client to create a tunnel interface.",
    },
    "kvm": {
        "path": "/dev/kvm:/dev/kvm:rwm",
        "label": "Nested virtualization",
        "help": "Hardware acceleration for VMs running inside this container.",
    },
    "fuse": {
        "path": "/dev/fuse:/dev/fuse:rwm",
        "label": "Userspace filesystems",
        "help": "Needed to mount FUSE filesystems such as sshfs.",
    },
}


class NetworkInterfaceCreate(BaseModel):
    """Schema for specifying a network interface during VM creation."""

    network_id: UUID
    ip_address: Optional[str] = Field(
        None,
        pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$",
        description="IP address in the network's subnet. Auto-assigned if not provided.",
    )


class NetworkInterfaceResponse(BaseModel):
    """Schema for network interface in API responses."""

    network_id: UUID
    network_name: str
    ip_address: str
    subnet: str
    is_primary: bool

    class Config:
        from_attributes = True


class VMBase(BaseModel):
    hostname: str = Field(..., min_length=1, max_length=63)
    ip_address: str = Field(..., pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
    cpu: int = Field(ge=1, le=32)
    ram_mb: int = Field(ge=512, le=131072)
    disk_gb: int = Field(ge=10, le=1000)
    position_x: int = Field(default=0)
    position_y: int = Field(default=0)


class VMCreate(BaseModel):
    """Schema for creating a new VM. ip_address is optional and auto-filled if not provided."""

    hostname: str = Field(..., min_length=1, max_length=63)
    ip_address: Optional[str] = Field(
        None,
        pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$",
        description="DEPRECATED: Use networks[0].ip_address instead. IP for primary network.",
    )
    cpu: int = Field(ge=1, le=32, default=2)
    ram_mb: int = Field(ge=512, le=131072, default=4096)
    disk_gb: int = Field(ge=10, le=1000, default=40)
    position_x: int = Field(default=0)
    position_y: int = Field(default=0)

    range_id: UUID
    network_id: Optional[UUID] = Field(
        None, description="DEPRECATED: Use networks field instead. Primary network ID."
    )

    # Multi-NIC support - list of network interfaces
    networks: Optional[List[NetworkInterfaceCreate]] = Field(
        None,
        description="Network interfaces for the VM. First is primary. Use this instead of network_id.",
    )

    # Image Library sources - exactly one required
    base_image_id: Optional[UUID] = None
    golden_image_id: Optional[UUID] = None
    snapshot_id: Optional[UUID] = None

    @model_validator(mode="after")
    def check_image_source(self) -> "VMCreate":
        """Ensure exactly one image source is provided."""
        sources = [
            self.base_image_id,
            self.golden_image_id,
            self.snapshot_id,
        ]
        set_count = sum(1 for s in sources if s is not None)

        if set_count == 0:
            raise ValueError(
                "Must provide exactly one of: base_image_id, golden_image_id, or snapshot_id"
            )
        if set_count > 1:
            raise ValueError(
                "Cannot specify multiple image sources. "
                "Provide exactly one of: base_image_id, golden_image_id, or snapshot_id"
            )

        return self

    @model_validator(mode="after")
    def check_networks(self) -> "VMCreate":
        """Validate network configuration and handle legacy format."""
        # If networks is provided, validate it
        if self.networks is not None:
            if len(self.networks) == 0:
                raise ValueError("networks array cannot be empty")

            # Check for duplicate network_ids
            network_ids = [n.network_id for n in self.networks]
            if len(network_ids) != len(set(network_ids)):
                raise ValueError("Duplicate network_id in networks array")

            return self

        # Legacy format: convert network_id to networks array
        if self.network_id is not None:
            self.networks = [
                NetworkInterfaceCreate(network_id=self.network_id, ip_address=self.ip_address)
            ]
            return self

        # Neither provided
        raise ValueError("Must provide either 'networks' array or legacy 'network_id' field")

    # Windows-specific settings (for dockur/windows VMs)
    # Version codes: 11, 11l, 11e, 10, 10l, 10e, 8e, 7u, vu, xp, 2k, 2025, 2022, 2019, 2016, 2012, 2008, 2003
    windows_version: Optional[str] = Field(
        None, max_length=10, description="Windows version code for dockur/windows"
    )
    windows_username: Optional[str] = Field(
        None, max_length=64, description="Windows username (default: Docker)"
    )
    windows_password: Optional[str] = Field(
        None, max_length=128, description="Windows password (default: empty)"
    )
    iso_url: Optional[str] = Field(None, max_length=512, description="Custom ISO download URL")
    iso_path: Optional[str] = Field(
        None, max_length=512, description="Local ISO path for bind mount"
    )
    display_type: Optional[str] = Field(
        "desktop", description="Display type: 'desktop' (VNC/web console) or 'server' (RDP only)"
    )

    # macOS-specific settings (for dockur/macos VMs)
    # Version codes: sequoia, sonoma, ventura, monterey, big-sur, catalina, mojave, high-sierra
    macos_version: Optional[str] = Field(
        None, max_length=20, description="macOS version code for dockur/macos"
    )

    # Network configuration
    use_dhcp: bool = Field(default=False, description="Use DHCP instead of static IP assignment")
    gateway: Optional[str] = Field(
        None, pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", description="Gateway IP address"
    )
    dns_servers: Optional[str] = Field(
        None, max_length=100, description="DNS servers (comma-separated)"
    )

    # Additional storage (appears as D:, E: drives in Windows)
    disk2_gb: Optional[int] = Field(None, ge=1, le=1000, description="Second disk size in GB")
    disk3_gb: Optional[int] = Field(None, ge=1, le=1000, description="Third disk size in GB")

    # Shared folders
    enable_shared_folder: bool = Field(
        default=False, description="Enable per-VM shared folder (/shared)"
    )
    enable_global_shared: bool = Field(
        default=False, description="Mount global shared folder (/global, read-only)"
    )

    # Localization
    language: Optional[str] = Field(
        None, max_length=50, description="Windows language (e.g., French, German)"
    )
    keyboard: Optional[str] = Field(
        None, max_length=20, description="Keyboard layout (e.g., en-US, de-DE)"
    )
    region: Optional[str] = Field(
        None, max_length=20, description="Regional settings (e.g., en-US, fr-FR)"
    )

    # Installation mode
    manual_install: bool = Field(
        default=False, description="Enable manual/interactive installation mode"
    )

    # Linux user configuration (for cloud-init in qemux/qemu, env vars in KasmVNC/LinuxServer)
    linux_username: Optional[str] = Field(None, max_length=64, description="Linux username")
    linux_password: Optional[str] = Field(None, max_length=128, description="Linux password")
    linux_user_sudo: bool = Field(
        default=True, description="Grant sudo/admin privileges to the user"
    )

    # Boot source for QEMU-based VMs (Windows via dockur, Linux via qemux)
    # golden_image = boot from pre-configured snapshot (fast)
    # fresh_install = boot from cached ISO (requires install)
    boot_source: Optional[Literal["golden_image", "fresh_install"]] = Field(
        None,
        description="Boot source for QEMU VMs: 'golden_image' (pre-configured) or 'fresh_install' (from ISO)",
    )

    # Target architecture for QEMU VMs
    # Defaults to host architecture if not specified
    arch: Optional[Literal["x86_64", "arm64"]] = Field(
        None, description="Target architecture: 'x86_64' or 'arm64'. Defaults to host architecture."
    )
    # Container environment variables, applied when the container is created.
    environment: Optional[Dict[str, str]] = Field(
        None, description="Environment variables passed to the container at creation time."
    )


class VMUpdate(BaseModel):
    hostname: Optional[str] = Field(None, min_length=1, max_length=63)
    ip_address: Optional[str] = Field(None, pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
    cpu: Optional[int] = Field(None, ge=1, le=32)
    ram_mb: Optional[int] = Field(None, ge=512, le=131072)
    disk_gb: Optional[int] = Field(None, ge=10, le=1000)
    position_x: Optional[int] = None
    position_y: Optional[int] = None
    # Windows settings can be updated
    windows_version: Optional[str] = Field(None, max_length=10)
    windows_username: Optional[str] = Field(None, max_length=64)
    windows_password: Optional[str] = Field(None, max_length=128)
    iso_url: Optional[str] = Field(None, max_length=512)
    iso_path: Optional[str] = Field(None, max_length=512)
    display_type: Optional[str] = Field(None, max_length=20)
    # macOS settings
    macos_version: Optional[str] = Field(None, max_length=20)
    # Network configuration
    use_dhcp: Optional[bool] = None
    gateway: Optional[str] = Field(None, pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
    dns_servers: Optional[str] = Field(None, max_length=100)
    # Extended configuration
    disk2_gb: Optional[int] = Field(None, ge=1, le=1000)
    disk3_gb: Optional[int] = Field(None, ge=1, le=1000)
    enable_shared_folder: Optional[bool] = None
    enable_global_shared: Optional[bool] = None
    language: Optional[str] = Field(None, max_length=50)
    keyboard: Optional[str] = Field(None, max_length=20)
    region: Optional[str] = Field(None, max_length=20)
    manual_install: Optional[bool] = None
    # Linux user configuration
    linux_username: Optional[str] = Field(None, max_length=64)
    linux_password: Optional[str] = Field(None, max_length=128)
    linux_user_sudo: Optional[bool] = None
    # Boot source for QEMU VMs
    boot_source: Optional[Literal["golden_image", "fresh_install"]] = None
    # Target architecture
    arch: Optional[Literal["x86_64", "arm64"]] = None
    # Container environment variables (desired state).
    # Editing this does NOT reach a running container — Docker bakes env at
    # create time. Call POST /vms/{id}/apply-config to recreate and apply.
    environment: Optional[Dict[str, str]] = None
    # Host devices to map in, as ALLOWED_DEVICES keys (e.g. ["tun"]). Same
    # create-time constraint as environment: apply-config recreates to take
    # effect. Unknown keys are rejected rather than ignored.
    devices: Optional[List[str]] = None


class VMResponse(VMBase):
    # Read model: echo stored sizes without re-imposing the interactive-create
    # minimums — blueprint-deployed VMs may be smaller (e.g. a 256 MB redis).
    cpu: int
    ram_mb: int
    disk_gb: int
    id: UUID
    range_id: UUID
    network_id: UUID  # Primary network ID (for backwards compatibility)
    # Multi-NIC support - all network interfaces
    networks: List[NetworkInterfaceResponse] = Field(default_factory=list)
    # Image Library sources
    base_image_id: Optional[UUID] = None
    golden_image_id: Optional[UUID] = None
    snapshot_id: Optional[UUID] = None
    status: VMStatus
    error_message: Optional[str] = None
    container_id: Optional[str] = None
    # Windows-specific fields
    windows_version: Optional[str] = None
    windows_username: Optional[str] = None
    # Note: windows_password not included in response for security
    iso_url: Optional[str] = None
    iso_path: Optional[str] = None
    display_type: Optional[str] = "desktop"
    # macOS-specific fields
    macos_version: Optional[str] = None
    # Network configuration
    use_dhcp: bool = False
    gateway: Optional[str] = None
    dns_servers: Optional[str] = None
    # Extended configuration
    disk2_gb: Optional[int] = None
    disk3_gb: Optional[int] = None
    enable_shared_folder: bool = False
    enable_global_shared: bool = False
    language: Optional[str] = None
    keyboard: Optional[str] = None
    region: Optional[str] = None
    manual_install: bool = False
    # Linux user configuration (password excluded for security)
    linux_username: Optional[str] = None
    linux_user_sudo: bool = True
    # Container environment variables (AVD/VPN target config, etc.) so the
    # range/console UI can read and edit them.
    environment: Optional[Dict[str, str]] = None
    # Boot source for QEMU VMs
    boot_source: Optional[str] = None
    # Target architecture
    arch: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    # Container environment: desired vs. what the live container was built with.
    environment: Optional[Dict[str, str]] = None
    applied_environment: Optional[Dict[str, str]] = None
    # Host devices: desired vs. what the live container was actually given.
    devices: Optional[List[str]] = None
    applied_devices: Optional[List[str]] = None

    # Multi-architecture support - computed by API
    emulated: bool = False
    emulation_warning: Optional[str] = None

    class Config:
        from_attributes = True

    @field_serializer("status")
    def serialize_status(self, status: VMStatus) -> str:
        """Return lowercase status for frontend compatibility."""
        return status.value.lower()

    @computed_field
    @property
    def environment_pending_keys(self) -> List[str]:
        """Env var names that differ between desired and applied state.

        Non-empty means the container is running with stale values and needs
        POST /vms/{id}/apply-config to reconcile. Covers added, changed, and
        removed keys — a removed key is still a change the container hasn't seen.

        A VM with no container yet has nothing applied, so nothing is "pending":
        its env will be picked up by the initial create. Reporting pending here
        would light up the badge on every freshly-added VM.
        """
        if not self.container_id:
            return []
        desired = self.environment or {}
        applied = self.applied_environment or {}
        return sorted(k for k in set(desired) | set(applied) if desired.get(k) != applied.get(k))

    @computed_field
    @property
    def devices_pending(self) -> bool:
        """True when the requested devices differ from what the container got.

        Devices are fixed when a container is created, exactly like environment,
        so the same rule applies: no container means nothing is pending, because
        the initial create will pick the list up.
        """
        if not self.container_id:
            return False
        return sorted(self.devices or []) != sorted(self.applied_devices or [])


class VMResourceUpdate(BaseModel):
    """Desired CPU and memory for a VM.

    Bounds match VMUpdate, and they are the only bounds there are: the Docker
    daemon accepts a NanoCpus of 1000 without complaint, so nothing downstream
    will catch an absurd value.

    Both fields are optional so a caller can change one without restating the
    other, but at least one must be present - an empty body is a mistake, not a
    no-op worth accepting.
    """

    cpu: Optional[int] = Field(None, ge=1, le=32)
    ram_mb: Optional[int] = Field(None, ge=512, le=131072)

    @model_validator(mode="after")
    def require_one(self) -> "VMResourceUpdate":
        if self.cpu is None and self.ram_mb is None:
            raise ValueError("Specify at least one of cpu, ram_mb")
        return self


class VMResourceUpdateResponse(BaseModel):
    """Outcome of a resource change.

    `applied_live` is the part a caller cannot infer: the new values are always
    saved, but they only reach a running container when there is one. When it is
    False, `detail` says what has to happen for them to take effect.
    """

    vm_id: UUID
    cpu: int
    ram_mb: int
    applied_live: bool
    detail: str
