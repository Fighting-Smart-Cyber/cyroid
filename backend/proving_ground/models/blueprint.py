# backend/proving_ground/models/blueprint.py
from typing import Optional, List
from uuid import UUID
from sqlalchemy import String, Text, ForeignKey, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from proving_ground.models.base import Base, TimestampMixin, UUIDMixin


class RangeBlueprint(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "range_blueprints"

    name: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    config: Mapped[dict] = mapped_column(JSON)  # networks, VMs, MSEL, router

    # Linked content for training events (auto-selected when blueprint chosen)
    content_ids: Mapped[Optional[List[str]]] = mapped_column(JSON, default=list)

    # Built-in blueprint identification (like templates)
    is_seed: Mapped[bool] = mapped_column(
        default=False
    )  # True for blueprints shipped with PROVING GROUND
    seed_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, unique=True
    )  # e.g., "red-team-training-lab"

    # Ownership (nullable for seed blueprints)
    created_by: Mapped[Optional[UUID]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_by_user = relationship("User", foreign_keys=[created_by])

    # Relationships
    instances: Mapped[List["RangeInstance"]] = relationship(
        "RangeInstance", back_populates="blueprint", cascade="all, delete-orphan"
    )


class RangeInstance(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "range_instances"

    name: Mapped[str] = mapped_column(String(100))
    blueprint_id: Mapped[UUID] = mapped_column(ForeignKey("range_blueprints.id"))
    blueprint_version: Mapped[int] = mapped_column(Integer)
    # Which instance of the blueprint this is -- a label, not an allocation. Era A took a subnet
    # per instance from a counter on the blueprint (`next_offset`); DinD isolation made that
    # unnecessary and PG-122 removed it. This survives because the UI numbers instances with it.
    subnet_offset: Mapped[int] = mapped_column(Integer, default=0)

    # Ownership
    instructor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    instructor = relationship("User", foreign_keys=[instructor_id])

    # Link to actual range
    range_id: Mapped[UUID] = mapped_column(ForeignKey("ranges.id"))
    range = relationship("Range")

    # Parent blueprint
    blueprint = relationship("RangeBlueprint", back_populates="instances")
