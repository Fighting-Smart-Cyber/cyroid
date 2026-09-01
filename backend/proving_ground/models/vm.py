# backend/proving_ground/models/vm.py
from enum import Enum
from typing import Optional, List, TYPE_CHECKING
from uuid import UUID
from sqlalchemy import String, Integer, ForeignKey, JSON, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from proving_ground.models.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from proving_ground.models.base_image import BaseImage
    from proving_ground.models.golden_image import GoldenImage
    from proving_ground.models.vm_network import VMNetwork
    from proving_ground.models.artifact import ArtifactPlacement
    from proving_ground.models.connection import Connection
    from proving_ground.models.event_log import EventLog
    from proving_ground.models.snapshot import Snapshot


class VMStatus(str, Enum):
    PENDING = "pending"
    CREATING = "creating"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


class BootSource(str, Enum):
    """Boot source for QEMU-based VMs (Windows via dockur, Linux via qemux)."""

    GOLDEN_IMAGE = "golden_image"  # Boot from pre-configured golden image (fast)
    FRESH_INSTALL = "fresh_install"  # Boot from cached ISO (requires manual/auto install)


class VM(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "vms"

    range_id: Mapped[UUID] = mapped_column(ForeignKey("ranges.id", ondelete="CASCADE"))
    network_id: Mapped[UUID] = mapped_column(ForeignKey("networks.id"))

    # Image source: exactly one of base_image_id, golden_image_id, or snapshot_id
    base_image_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("base_images.id", ondelete="SET NULL"), nullable=True
    )
    golden_image_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("golden_images.id", ondelete="SET NULL"), nullable=True
    )
    snapshot_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("snapshots.id"), nullable=True)

    hostname: Mapped[str] = mapped_column(String(63))
    ip_address: Mapped[str] = mapped_column(String(15))

    # Specs (can override template defaults)
    cpu: Mapped[int] = mapped_column(Integer)
    ram_mb: Mapped[int] = mapped_column(Integer)
    disk_gb: Mapped[int] = mapped_column(Integer)

    status: Mapped[VMStatus] = mapped_column(default=VMStatus.PENDING)

    # Error tracking
    error_message: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    # Docker container ID (set after creation)
    container_id: Mapped[Optional[str]] = mapped_column(String(64))

    # Windows-specific settings (for dockur/windows VMs)
    # Version codes: 11, 11l, 11e, 10, 10l, 10e, 8e, 7u, vu, xp, 2k, 2025, 2022, 2019, 2016, 2012, 2008, 2003
    windows_version: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    windows_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    windows_password: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    iso_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    iso_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    # Display type for Windows VMs: desktop (VNC/web console) or server (RDP only)
    display_type: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, default="desktop"
    )

    # macOS-specific settings (for dockur/macos VMs)
    # Version codes: sequoia, sonoma, ventura, monterey, big-sur, catalina, mojave, high-sierra
    macos_version: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # Network configuration
    use_dhcp: Mapped[bool] = mapped_column(Boolean, default=False)  # DHCP vs static IP
    gateway: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)  # Gateway IP
    dns_servers: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )  # Comma-separated DNS

    # Additional storage (shows as D:, E: drives in Windows)
    disk2_gb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    disk3_gb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Shared folders (bind mounts)
    enable_shared_folder: Mapped[bool] = mapped_column(Boolean, default=False)  # Per-VM /shared
    enable_global_shared: Mapped[bool] = mapped_column(
        Boolean, default=False
    )  # Global /global (read-only)

    # Localization
    language: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )  # e.g., "French", "German"
    keyboard: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True
    )  # e.g., "en-US", "de-DE"
    region: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True
    )  # e.g., "en-US", "fr-FR"

    # Installation mode
    manual_install: Mapped[bool] = mapped_column(Boolean, default=False)  # Interactive install mode

    # Linux VM-specific settings (for qemux/qemu VMs)
    # Distro codes: ubuntu, debian, fedora, alpine, arch, manjaro, opensuse, mint,
    #               zorin, elementary, popos, kali, parrot, tails, rocky, alma
    linux_distro: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    boot_mode: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True, default="uefi"
    )  # uefi or legacy
    disk_type: Mapped[Optional[str]] = mapped_column(
        String(10), nullable=True, default="scsi"
    )  # scsi, blk, or ide

    # Linux user configuration (for cloud-init in qemux/qemu, env vars in KasmVNC/LinuxServer)
    linux_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    linux_password: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    linux_user_sudo: Mapped[bool] = mapped_column(Boolean, default=True)

    # Boot source for QEMU VMs (golden_image = fast clone, fresh_install = boot from ISO)
    # Only used for Windows/Linux VM types (not container-based VMs)
    boot_source: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, default=None)

    # Target architecture for QEMU VMs (x86_64 or arm64)
    # Defaults to host architecture if not specified
    # Used to select correct cached ISO and determine if emulation is needed
    arch: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, default=None)

    # Environment variables passed to the container at runtime.
    # `environment` is the DESIRED state, editable at any time.
    # `applied_environment` is what the live container was actually created with.
    # Docker bakes env at container-create time and never re-reads it on restart,
    # so the two diverge whenever `environment` is edited on a provisioned VM.
    # The difference is what drives the "pending changes" indicator in the UI and
    # is reconciled only by POST /vms/{id}/apply-config, which recreates the
    # container. Do not set `applied_environment` anywhere except at container
    # creation, or the pending state will silently lie.
    environment: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    applied_environment: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Host devices mapped into the container, as a list of ALLOWED_DEVICES keys
    # (e.g. ["tun"]) - never raw host paths. Device passthrough is host-level
    # access handed to whoever can reach the container, so the key is resolved
    # to a path server-side against a fixed allow-list; an arbitrary path from a
    # client is rejected, not mapped.
    #
    # Same desired/applied split as `environment` and for the same reason:
    # devices are fixed when the container is created and cannot be added to a
    # running one. `applied_devices` records what the live container actually
    # got, and is written only at container creation.
    devices: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    applied_devices: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Position in visual builder (for UI)
    position_x: Mapped[int] = mapped_column(Integer, default=0)
    position_y: Mapped[int] = mapped_column(Integer, default=0)

    # Relationships
    range = relationship("Range", back_populates="vms")
    network = relationship("Network", back_populates="vms")
    # Image Library relationships
    base_image: Mapped[Optional["BaseImage"]] = relationship(
        "BaseImage", back_populates="vms", foreign_keys=[base_image_id]
    )
    golden_image: Mapped[Optional["GoldenImage"]] = relationship(
        "GoldenImage", back_populates="vms", foreign_keys=[golden_image_id]
    )
    snapshots: Mapped[List["Snapshot"]] = relationship(
        "Snapshot", back_populates="vm", foreign_keys="[Snapshot.vm_id]"
    )
    source_snapshot: Mapped[Optional["Snapshot"]] = relationship(
        "Snapshot", back_populates="created_vms", foreign_keys="[VM.snapshot_id]"
    )
    artifact_placements: Mapped[List["ArtifactPlacement"]] = relationship(
        "ArtifactPlacement", back_populates="vm", cascade="all, delete-orphan"
    )
    event_logs: Mapped[List["EventLog"]] = relationship(
        "EventLog", back_populates="vm", cascade="all, delete-orphan"
    )
    outgoing_connections: Mapped[List["Connection"]] = relationship(
        "Connection", foreign_keys="Connection.src_vm_id", back_populates="src_vm"
    )
    incoming_connections: Mapped[List["Connection"]] = relationship(
        "Connection", foreign_keys="Connection.dst_vm_id", back_populates="dst_vm"
    )
    # Multi-NIC support: all network interfaces for this VM
    network_interfaces: Mapped[List["VMNetwork"]] = relationship(
        "VMNetwork", back_populates="vm", cascade="all, delete-orphan"
    )

    # ------------------------------------------------------------------
    # "Which image is this VM from?"
    #
    # That question was answered independently at a dozen call sites, each
    # reaching for vm.base_image and so treating a golden-image VM as a plain
    # container. Every one of those was a live defect before it was found:
    #
    #   deploy      "VM has no container image configured"
    #   sync        the same, plus UnboundLocalError on base_img
    #   pull list   the pinned runtime was never fetched into the range
    #   console     noVNC pointed at the HTML page instead of websockify
    #   console     the QEMU port used for a container VM
    #   snapshots   a Windows VM recorded as a linux container
    #
    # Precedence is base image, then golden image, then the snapshot the VM
    # was created from: most specific provenance first.
    # ------------------------------------------------------------------

    @property
    def effective_image(self):
        """The image record this VM was actually created from, or None."""
        return self.base_image or self.golden_image or self.source_snapshot

    @property
    def effective_vm_type(self) -> Optional[str]:
        """vm_type from whichever image source this VM has."""
        image = self.effective_image
        return getattr(image, "vm_type", None) if image is not None else None

    @property
    def effective_os_type(self) -> Optional[str]:
        """os_type from whichever image source this VM has."""
        image = self.effective_image
        return getattr(image, "os_type", None) if image is not None else None
