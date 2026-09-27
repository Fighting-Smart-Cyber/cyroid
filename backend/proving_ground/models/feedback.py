"""Customer feedback: platform ideas and defects, and problems with content or a range.

Built for a named, invited audience in preprod -- a handful of evaluators and instructors -- not
for a public crowd. That shapes what is here and, more, what is not: no votes (a popularity
contest needs a population), no automated triage states nothing writes, no duplicate detection
for a volume that will not occur this PI.

The one idea taken verbatim from COSMOS is the newest one in that codebase: `source` and
`source_ref` as indexed columns rather than prose. Its own migration says why -- everything
raised from a guided walkthrough used to land with its tour and step written into the
description, "readable, but not filterable... prose is the wrong place to keep the answer."
"""

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from proving_ground.models.base import Base, TimestampMixin, UUIDMixin


class FeedbackKind(str, Enum):
    """What the person is telling us, and -- because of that -- who reads it.

    Four values rather than COSMOS's two. Its enum is BUG|FEATURE, and its own code apologises
    in a comment for filing "Question" as a feature request. That holds for a project-management
    tool; it breaks here, because "this step's command is wrong" has to reach the person who
    writes content, not the person who writes the platform.
    """

    BUG = "bug"
    IDEA = "idea"
    CONTENT_PROBLEM = "content_problem"
    RANGE_PROBLEM = "range_problem"


class FeedbackStatus(str, Enum):
    OPEN = "open"
    TRIAGED = "triaged"
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    DECLINED = "declined"


class FeedbackSource(str, Enum):
    """Where in the product it was raised, so a guide's feedback can be found as a group."""

    APP = "app"
    WALKTHROUGH = "walkthrough"
    RANGE = "range"
    CONSOLE = "console"


class Feedback(Base, UUIDMixin, TimestampMixin):
    """One thing somebody told us."""

    __tablename__ = "feedback"

    author_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    author = relationship("User")

    kind: Mapped[FeedbackKind] = mapped_column(default=FeedbackKind.BUG, index=True)
    status: Mapped[FeedbackStatus] = mapped_column(default=FeedbackStatus.OPEN, index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text)

    source: Mapped[FeedbackSource] = mapped_column(default=FeedbackSource.APP)
    #: The address within that source. `<phase_id>/<step_id>` for a walkthrough step, a machine
    #: name for a console. Prefix-matchable, so "everything raised against this guide" is a
    #: query rather than a read-through.
    source_ref: Mapped[Optional[str]] = mapped_column(String(200))

    #: What it is about, when it is about something. SET NULL rather than CASCADE: a range is
    #: torn down at the end of every exercise and the report outlives it.
    range_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("ranges.id", ondelete="SET NULL"), nullable=True, index=True
    )
    content_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("content.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: The snapshot, frozen at submission: names, versions, the route, the app version. The FKs
    #: above are for querying; this is so a report still reads when what it names is gone.
    context: Mapped[dict] = mapped_column(JSON, default=dict)

    #: Set when this has been carried onto the engineering board, with the id it got there.
    #: CYROID has no board of its own -- the board is COSMOS project PG -- so delivery is a
    #: separate, distribution-side concern. These two columns are what lets it be a script
    #: later rather than a migration later.
    external_ref: Mapped[Optional[str]] = mapped_column(String(100))
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_feedback_source_ref", "source", "source_ref"),
        Index("ix_feedback_status_created", "status", "created_at"),
    )
