# backend/proving_ground/models/walkthrough_progress.py
from typing import Any, Dict, Optional, List, TYPE_CHECKING
from uuid import UUID
from sqlalchemy import String, ForeignKey, UniqueConstraint, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from proving_ground.models.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from proving_ground.models.range import Range
    from proving_ground.models.user import User


class WalkthroughProgress(Base, UUIDMixin, TimestampMixin):
    """Tracks student progress through a walkthrough."""

    __tablename__ = "walkthrough_progress"

    range_id: Mapped[UUID] = mapped_column(
        ForeignKey("ranges.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    completed_steps: Mapped[List[str]] = mapped_column(JSON, default=list)
    current_phase: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    current_step: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Knowledge Check answers, keyed by question id:
    #
    #     {"<question-id>": {"question_id", "step_id", "selected_option_id",
    #                        "correct", "correct_option_id", "explanation",
    #                        "answered_at"}}
    #
    # Marking a step complete said nothing about whether the learner got it
    # right, so a quiz score reached nobody and nothing: the assessment left no
    # trace at all. This is the smallest record that answers what was asked,
    # what was chosen, whether it was right and when -- and it sits here
    # because the row is already unique per (range, user), which is the same
    # key an attempt has.
    quiz_answers: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)

    # Relationships
    range: Mapped["Range"] = relationship("Range")
    user: Mapped["User"] = relationship("User")

    __table_args__ = (
        UniqueConstraint("range_id", "user_id", name="uq_walkthrough_progress_range_user"),
    )
