# backend/proving_ground/models/range.py
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Optional, List
from uuid import UUID
from sqlalchemy import String, Text, ForeignKey, DateTime, JSON, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from proving_ground.models.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from proving_ground.models.connection import Connection
    from proving_ground.models.event_log import EventLog
    from proving_ground.models.msel import MSEL
    from proving_ground.models.network import Network
    from proving_ground.models.router import RangeRouter
    from proving_ground.models.vm import VM


class RangeStatus(str, Enum):
    DRAFT = "draft"
    DEPLOYING = "deploying"
    RUNNING = "running"
    STOPPED = "stopped"
    ARCHIVED = "archived"
    ERROR = "error"


class RangeVisibility(str, Enum):
    """Who may see a range.

    Before this existed, visibility was inferred: a range with no tags was
    treated as public, so every range anyone created was visible to every
    non-student account. That was a default nobody chose, not a decision.

    PRIVATE is the default now. Sharing is something you do, not something
    that happens because you did not do anything.
    """

    PRIVATE = "private"  # owner and admins only
    SHARED = "shared"  # plus named users and matching tags
    PUBLIC = "public"  # any authenticated user


class RangeShare(Base, UUIDMixin, TimestampMixin):
    """One person granted sight of one range.

    Named users and tags are both grant paths for a SHARED range: a person is
    for "let Jon see this", a tag is for a cohort. Neither implies control --
    sharing shows a range, it does not hand over the power to tear it down.
    """

    __tablename__ = "range_shares"

    range_id: Mapped[UUID] = mapped_column(ForeignKey("ranges.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Who granted it, kept for the audit question "why can this person see it".
    granted_by: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:
        return f"<RangeShare range={self.range_id} user={self.user_id}>"


class Range(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "ranges"

    name: Mapped[str] = mapped_column(String(100), index=True)
    # Stored as the enum VALUE ("private"), not its NAME ("PRIVATE").
    #
    # SQLAlchemy infers an Enum type from Mapped[RangeVisibility] and persists
    # member NAMES by default. The migration that added this column declared it
    # String(20) with server_default="private" -- the value -- so the two
    # disagreed: rows the ORM wrote read back fine, rows the server_default
    # wrote raised LookupError on hydration and took the whole range list with
    # them. values_callable settles it on the value, which is also what the API
    # puts on the wire and what the frontend's RangeVisibility union expects.
    visibility: Mapped[RangeVisibility] = mapped_column(
        SAEnum(
            RangeVisibility,
            native_enum=False,
            create_constraint=False,
            length=20,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        default=RangeVisibility.PRIVATE,
        server_default="private",
        nullable=False,
    )
    description: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[RangeStatus] = mapped_column(default=RangeStatus.DRAFT)

    # Error tracking
    error_message: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    # Lifecycle timestamps
    deployed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # DinD (Docker-in-Docker) container tracking for network isolation
    dind_container_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    dind_container_name: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    dind_mgmt_ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)  # IPv4 or IPv6
    dind_docker_url: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Recorded because a warm-pool container's volume is created before the
    # range exists, so its name cannot be derived from the range id.
    dind_volume_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # VNC proxy port mappings (JSON: {vm_id: {proxy_host, proxy_port, original_port}})
    vnc_proxy_mappings: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Ownership
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_by_user = relationship("User", back_populates="ranges", foreign_keys=[created_by])

    # Per-student assignment (for training events)
    assigned_to_user_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assigned_to_user = relationship("User", foreign_keys=[assigned_to_user_id])

    # Training event link (for auto-deployed ranges)
    training_event_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("training_events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    training_event = relationship("TrainingEvent", foreign_keys=[training_event_id])

    # Training content link (from Content Library)
    student_guide_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("content.id", ondelete="SET NULL"), nullable=True
    )
    student_guide = relationship("Content", foreign_keys=[student_guide_id])

    # VM Console visibility control - VMs hidden from assigned user
    # Empty list = all VMs visible (default)
    hidden_vm_ids: Mapped[List[UUID]] = mapped_column(JSON, default=list)

    # Relationships
    networks: Mapped[List["Network"]] = relationship(
        "Network", back_populates="range", cascade="all, delete-orphan"
    )
    vms: Mapped[List["VM"]] = relationship(
        "VM", back_populates="range", cascade="all, delete-orphan"
    )
    event_logs: Mapped[List["EventLog"]] = relationship(
        "EventLog", back_populates="range", cascade="all, delete-orphan"
    )
    connections: Mapped[List["Connection"]] = relationship(
        "Connection", back_populates="range", cascade="all, delete-orphan"
    )
    msel: Mapped[Optional["MSEL"]] = relationship(
        "MSEL", back_populates="range", uselist=False, cascade="all, delete-orphan"
    )
    router: Mapped[Optional["RangeRouter"]] = relationship(
        "RangeRouter", back_populates="range", uselist=False, cascade="all, delete-orphan"
    )
