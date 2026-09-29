# backend/proving_ground/models/golden_image.py
"""GoldenImage model - represents pre-configured VM images in the Image Library.

GoldenImages are created from:
- First snapshot of any VM (linked to its BaseImage)
- Imported OVA/QCOW2/VMDK files

They track lineage back to the original BaseImage when applicable.
"""

from enum import Enum
from typing import Optional, List, TYPE_CHECKING
from uuid import UUID
from sqlalchemy import String, Integer, BigInteger, Text, ForeignKey, JSON, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from proving_ground.models.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from proving_ground.models.base_image import BaseImage
    from proving_ground.models.snapshot import Snapshot
    from proving_ground.models.vm import VM
    from proving_ground.models.user import User


class GoldenImageSource(str, Enum):
    """Source of the golden image."""

    SNAPSHOT = "snapshot"  # First snapshot of a VM
    IMPORT = "import"  # Imported OVA/QCOW2/VMDK


class GoldenImage(Base, UUIDMixin, TimestampMixin):
    """Golden image in the Image Library.

    Golden images are pre-configured VM images that can be used to quickly
    spin up new VMs without going through full installation.

    Created from:
    - First snapshot of a VM (source='snapshot')
    - Imported VM images (source='import')

    Tracks lineage to the original base image when applicable.
    """

    __tablename__ = "golden_images"

    name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Source tracking (lineage)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # snapshot or import
    base_image_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("base_images.id", ondelete="SET NULL"), nullable=True
    )
    source_vm_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("vms.id", ondelete="SET NULL"), nullable=True
    )

    # Storage - Container snapshots
    docker_image_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )  # sha256:hash
    docker_image_tag: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )  # e.g., proving-ground-golden:dc01-v1

    # The runtime this disk was captured against, as a pinnable reference
    # (e.g. dockurr/windows@sha256:...). A disk-based golden image is not
    # self-contained: it is an installed OS that expects the virtual hardware
    # it was installed on. Deploying it against a newer dockur/QEMU changes
    # that hardware and Windows boots into Automatic Repair -- observed live,
    # QEMU 10.0.11 at capture vs 11.1.0 on the clone. Pinning keeps a captured
    # image reproducible, and matches the digest rule in ADR-0007.
    runtime_image_digest: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Storage - Imported disk images
    disk_image_path: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )  # Path to qcow2 file
    import_format: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True
    )  # ova, qcow2, vmdk, vdi

    # Metadata
    os_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # windows, linux, network, custom
    vm_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # container, linux_vm, windows_vm
    native_arch: Mapped[str] = mapped_column(String(20), default="x86_64")  # x86_64, arm64

    # Resource defaults (inherited from base image or configured on import)
    default_cpu: Mapped[int] = mapped_column(Integer, default=2)
    default_ram_mb: Mapped[int] = mapped_column(Integer, default=4096)
    default_disk_gb: Mapped[int] = mapped_column(Integer, default=40)

    # Display configuration
    display_type: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True
    )  # desktop, headless
    vnc_port: Mapped[int] = mapped_column(Integer, default=8006)

    # Size tracking
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # Visibility and ownership
    is_global: Mapped[bool] = mapped_column(Boolean, default=True)  # Visible to all users
    # ondelete matches the database: deleting a user nulls their authorship
    # rather than blocking the delete. Declared here so autogenerate does not
    # keep proposing to recreate this FK without it.
    created_by: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Categorization
    tags: Mapped[List[str]] = mapped_column(JSON, default=list)

    # Relationships
    base_image: Mapped[Optional["BaseImage"]] = relationship(
        "BaseImage", back_populates="golden_images"
    )
    snapshots: Mapped[List["Snapshot"]] = relationship("Snapshot", back_populates="golden_image")
    vms: Mapped[List["VM"]] = relationship(
        "VM", back_populates="golden_image", foreign_keys="[VM.golden_image_id]"
    )
    source_vm: Mapped[Optional["VM"]] = relationship(
        "VM", foreign_keys=[source_vm_id], overlaps="golden_image,vms"
    )
    created_by_user: Mapped[Optional["User"]] = relationship("User", back_populates="golden_images")
