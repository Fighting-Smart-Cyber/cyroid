"""Telling us something: an idea, a defect, or a problem with a guide or a range.

Built for customers invited into preprod. Two things shape it:

**The context is read, not asked.** COSMOS asks "which project is this about?", which is the one
question a learner standing in a broken lab cannot answer. What they are doing is already known
-- the guide, the step, the range -- so the client sends it and the submitter sees it before
they send, as a line they can clear. Asked-for context is wrong twice: it is work for the person
least able to do it, and the answer is worse than the one already in hand.

**It is a private channel to the vendor.** COSMOS makes every item readable by every member of
the org. Here, content feedback says "I could not finish this lab" about a learner whose
demonstrated competence is the product's output, and a queue that doubles as a list of who
struggled is not a queue anybody writes candidly into. An author sees their own; staff see all.
"""

from typing import Annotated, Any, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from proving_ground.api.deps import CurrentUser, DBSession
from proving_ground.models.feedback import (
    Feedback,
    FeedbackKind,
    FeedbackSource,
    FeedbackStatus,
)
from proving_ground.models.user import User

router = APIRouter(tags=["feedback"])

#: Who may read everything and change a status. The same pair that may author content -- the
#: people who act on this -- rather than a new role nobody is assigned.
STAFF_ROLES = ("admin", "engineer")

#: Kept deliberately small. It is a frozen snapshot for reading a report after the thing it
#: names is gone, not a telemetry channel.
_CONTEXT_KEYS = {"route", "app_version", "range_name", "content_title", "content_version", "step"}


def is_staff(user: User) -> bool:
    return bool(user.is_admin or user.has_any_role(*STAFF_ROLES))


class FeedbackCreate(BaseModel):
    kind: FeedbackKind = FeedbackKind.BUG
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=5000)
    source: FeedbackSource = FeedbackSource.APP
    source_ref: Optional[str] = Field(default=None, max_length=200)
    range_id: Optional[UUID] = None
    content_id: Optional[UUID] = None
    context: dict[str, Any] = Field(default_factory=dict)


class FeedbackStatusUpdate(BaseModel):
    status: FeedbackStatus


class FeedbackResponse(BaseModel):
    id: UUID
    kind: FeedbackKind
    status: FeedbackStatus
    title: str
    description: Optional[str]
    source: FeedbackSource
    source_ref: Optional[str]
    range_id: Optional[UUID]
    content_id: Optional[UUID]
    context: dict[str, Any]
    author_id: UUID
    created_at: Any
    updated_at: Any = None
    # Who sent it, as a name rather than a UUID. Triage is a person reading a list, and an
    # id there means a second query per row before it means anything. Computed rather than
    # stored: the row already carries the relationship.
    author_username: Optional[str] = None

    class Config:
        from_attributes = True

    @staticmethod
    def of(item: Feedback) -> "FeedbackResponse":
        out = FeedbackResponse.model_validate(item)
        out.author_username = item.author.username if item.author else None
        return out


def _clean_context(raw: dict[str, Any]) -> dict[str, Any]:
    """Only the keys we said we would keep, each a short string.

    The client decides what to send and the client is the browser, so this is the boundary: an
    unbounded JSON blob from a submitter is a place to put things nobody reviewed.
    """
    return {k: str(v)[:200] for k, v in raw.items() if k in _CONTEXT_KEYS and v is not None}


def _visible(db: Session, user: User):
    query = db.query(Feedback)
    return query if is_staff(user) else query.filter(Feedback.author_id == user.id)


@router.post("/feedback", response_model=FeedbackResponse, status_code=status.HTTP_201_CREATED)
def submit_feedback(payload: FeedbackCreate, db: DBSession, current_user: CurrentUser):
    """Record it. Anyone signed in may tell us something -- that is the point of the channel."""
    item = Feedback(
        author_id=current_user.id,
        kind=payload.kind,
        title=payload.title.strip(),
        description=(payload.description or "").strip() or None,
        source=payload.source,
        source_ref=payload.source_ref,
        range_id=payload.range_id,
        content_id=payload.content_id,
        context=_clean_context(payload.context),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return FeedbackResponse.of(item)


@router.get("/feedback", response_model=list[FeedbackResponse])
def list_feedback(
    db: DBSession,
    current_user: CurrentUser,
    kind: Optional[FeedbackKind] = None,
    # Annotated rather than a Query() default: a bare Query() default is a Query OBJECT
    # when the function is called directly, which is how these are unit tested.
    feedback_status: Annotated[Optional[FeedbackStatus], Query(alias="status")] = None,
    content_id: Optional[UUID] = None,
    range_id: Optional[UUID] = None,
    source_ref_prefix: Optional[str] = None,
):
    """What this account may see. An author sees their own; staff see everything.

    `source_ref_prefix` is what makes a guide's feedback findable as a group: every step of one
    walkthrough shares its phase prefix, so asking for it returns the lot.
    """
    query = _visible(db, current_user)
    if kind is not None:
        query = query.filter(Feedback.kind == kind)
    if feedback_status is not None:
        query = query.filter(Feedback.status == feedback_status)
    if content_id is not None:
        query = query.filter(Feedback.content_id == content_id)
    if range_id is not None:
        query = query.filter(Feedback.range_id == range_id)
    if source_ref_prefix:
        query = query.filter(Feedback.source_ref.startswith(source_ref_prefix))
    return [FeedbackResponse.of(f) for f in query.order_by(Feedback.created_at.desc()).all()]


@router.get("/feedback/{feedback_id}", response_model=FeedbackResponse)
def get_feedback(feedback_id: UUID, db: DBSession, current_user: CurrentUser):
    item = _visible(db, current_user).filter(Feedback.id == feedback_id).first()
    if not item:
        # 404 rather than 403: whether somebody else's report exists is not this account's
        # business either way, and the two answers should not be distinguishable.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")
    return FeedbackResponse.of(item)


@router.patch("/feedback/{feedback_id}", response_model=FeedbackResponse)
def set_feedback_status(
    feedback_id: UUID,
    payload: FeedbackStatusUpdate,
    db: DBSession,
    current_user: CurrentUser,
):
    """Move it along. Staff only -- an author may report, not decide."""
    if not is_staff(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an engineer or an administrator may change a report's status.",
        )
    item = db.query(Feedback).filter(Feedback.id == feedback_id).first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")
    item.status = payload.status
    db.commit()
    db.refresh(item)
    return FeedbackResponse.of(item)
