# backend/proving_ground/api/walkthrough.py
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from pydantic import BaseModel

from proving_ground.api.deps import check_range_access, get_db, get_current_user
from proving_ground.models.user import User
from proving_ground.models.content import Content
from proving_ground.models.walkthrough_progress import WalkthroughProgress
from proving_ground.schemas.content import (
    LearnerWalkthroughSchema,
    QuizAnswerRecordSchema,
)
from proving_ground.services.walkthrough_parser import parse_markdown_to_walkthrough


router = APIRouter(prefix="/ranges", tags=["walkthrough"])


class WalkthroughResponse(BaseModel):
    # Typed, not a bare dict: the learner schema has no field an answer key
    # could occupy, so the stripping below is belt and the schema is braces.
    walkthrough: Optional[LearnerWalkthroughSchema] = None


class QuizAnswerSubmit(BaseModel):
    question_id: str
    option_id: str


class WalkthroughProgressResponse(BaseModel):
    range_id: UUID
    user_id: UUID
    completed_steps: List[str]
    current_phase: Optional[str]
    current_step: Optional[str]
    updated_at: str

    class Config:
        from_attributes = True


class WalkthroughProgressUpdate(BaseModel):
    completed_steps: List[str]
    current_phase: Optional[str] = None
    current_step: Optional[str] = None


def _load_progress(range_id: UUID, user_id: UUID, db: Session) -> Optional[WalkthroughProgress]:
    return (
        db.query(WalkthroughProgress)
        .filter(
            WalkthroughProgress.range_id == range_id,
            WalkthroughProgress.user_id == user_id,
        )
        .first()
    )


def _range_walkthrough(range_obj, db: Session) -> Optional[dict]:
    """The walkthrough a range presents, structured or parsed from markdown.

    One definition for both the delivery route and the grading route, so a
    question the learner was shown is a question the grader can find.
    """
    if not range_obj.student_guide_id:
        return None

    content = db.query(Content).filter(Content.id == range_obj.student_guide_id).first()
    if not content:
        return None

    if content.walkthrough_data:
        return content.walkthrough_data
    if content.body_markdown:
        # Auto-parse markdown into structured walkthrough format. Markdown
        # carries no quiz, so a guide delivered this way simply has none.
        return parse_markdown_to_walkthrough(content.title or "Walkthrough", content.body_markdown)
    return None


def find_quiz_question(walkthrough: dict, question_id: str):
    """Locate an authored question by id. Returns (phase, step, question).

    Question ids are expected to be unique within a walkthrough -- the schema
    gives every question an id precisely so it can be referred to from outside
    the document. The first match wins if an author duplicates one.
    """
    for phase in (walkthrough or {}).get("phases", []) or []:
        for step in phase.get("steps", []) or []:
            for question in step.get("quiz", []) or []:
                if question.get("id") == question_id:
                    return phase, step, question
    return None, None, None


def strip_quiz_answers(walkthrough: Optional[dict], answers: Optional[dict]) -> Optional[dict]:
    """Remove the answer key from a walkthrough before it reaches a browser.

    Every option arrived carrying its own `correct` flag, so the key to the
    Knowledge Check was in the page source before the learner picked anything
    -- readable in devtools, and enough on its own to make a score
    indefensible as evidence of competence.

    A question this learner has already answered gets that one answer back
    under `answered`, so reloading the page shows the same feedback they
    already saw rather than a blank quiz they could retake.
    """
    if not walkthrough:
        return walkthrough

    answers = answers or {}
    sanitised = deepcopy(walkthrough)

    for phase in sanitised.get("phases", []) or []:
        for step in phase.get("steps", []) or []:
            for question in step.get("quiz", []) or []:
                question.pop("explanation", None)
                for option in question.get("options", []) or []:
                    option.pop("correct", None)
                recorded = answers.get(question.get("id"))
                if recorded:
                    question["answered"] = recorded

    return sanitised


@router.get("/{range_id}/walkthrough", response_model=WalkthroughResponse)
def get_walkthrough(
    range_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    """Get the walkthrough content for a range from Content Library."""
    # Read access, not control: a learner assigned to the lab is exactly who
    # this route is for, and they never control the range.
    range_obj = check_range_access(range_id, current_user, db)

    walkthrough = _range_walkthrough(range_obj, db)

    progress = _load_progress(range_id, current_user.id, db)
    answers = progress.quiz_answers if progress else None
    return WalkthroughResponse(walkthrough=strip_quiz_answers(walkthrough, answers))


@router.get(
    "/{range_id}/walkthrough/progress", response_model=Optional[WalkthroughProgressResponse]
)
def get_walkthrough_progress(
    range_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    """Get the user's progress through the walkthrough."""
    check_range_access(range_id, current_user, db)

    progress = _load_progress(range_id, current_user.id, db)
    if not progress:
        return None

    return WalkthroughProgressResponse(
        range_id=progress.range_id,
        user_id=progress.user_id,
        completed_steps=progress.completed_steps or [],
        current_phase=progress.current_phase,
        current_step=progress.current_step,
        updated_at=progress.updated_at.isoformat(),
    )


@router.put("/{range_id}/walkthrough/progress", response_model=WalkthroughProgressResponse)
def update_walkthrough_progress(
    range_id: UUID,
    data: WalkthroughProgressUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update the user's progress through the walkthrough."""
    # The row is keyed to current_user below, so this writes nobody else's
    # record. Access is still required: progress against a range you cannot
    # see is a record of work you were never given.
    check_range_access(range_id, current_user, db)

    progress = _load_progress(range_id, current_user.id, db)

    if progress:
        progress.completed_steps = data.completed_steps
        progress.current_phase = data.current_phase
        progress.current_step = data.current_step
    else:
        progress = WalkthroughProgress(
            range_id=range_id,
            user_id=current_user.id,
            completed_steps=data.completed_steps,
            current_phase=data.current_phase,
            current_step=data.current_step,
        )
        db.add(progress)

    db.commit()
    db.refresh(progress)

    return WalkthroughProgressResponse(
        range_id=progress.range_id,
        user_id=progress.user_id,
        completed_steps=progress.completed_steps or [],
        current_phase=progress.current_phase,
        current_step=progress.current_step,
        updated_at=progress.updated_at.isoformat(),
    )


@router.post("/{range_id}/walkthrough/quiz", response_model=QuizAnswerRecordSchema)
def submit_quiz_answer(
    range_id: UUID,
    data: QuizAnswerSubmit,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Grade one Knowledge Check answer and write it to the learner record.

    Grading happens here rather than in the browser because the browser is the
    one party to an assessment whose answer cannot be taken on trust. The
    answer is written before it is returned, so a result the learner saw is a
    result the record holds.
    """
    range_obj = check_range_access(range_id, current_user, db)

    walkthrough = _range_walkthrough(range_obj, db)
    _, step, question = find_quiz_question(walkthrough, data.question_id)
    if question is None:
        raise HTTPException(
            status_code=404,
            detail="This range's walkthrough has no question with that id",
        )

    options = question.get("options", []) or []
    chosen = next((o for o in options if o.get("id") == data.option_id), None)
    if chosen is None:
        raise HTTPException(status_code=400, detail="That option is not one of the answers offered")

    progress = _load_progress(range_id, current_user.id, db)
    existing = (progress.quiz_answers or {}).get(data.question_id) if progress else None
    if existing:
        # One attempt per question. Re-answering would turn the Knowledge
        # Check into something a learner can grind until it passes, and the
        # record would show the last guess rather than the first answer.
        raise HTTPException(status_code=409, detail="This question has already been answered")

    correct_option = next((o for o in options if o.get("correct")), None)
    record = QuizAnswerRecordSchema(
        question_id=data.question_id,
        step_id=(step or {}).get("id"),
        selected_option_id=data.option_id,
        correct=bool(chosen.get("correct")),
        correct_option_id=(correct_option or {}).get("id"),
        explanation=question.get("explanation"),
        answered_at=datetime.now(timezone.utc).isoformat(),
    )

    if progress is None:
        progress = WalkthroughProgress(
            range_id=range_id,
            user_id=current_user.id,
            completed_steps=[],
            quiz_answers={},
        )
        db.add(progress)

    answers = dict(progress.quiz_answers or {})
    answers[data.question_id] = record.model_dump()
    progress.quiz_answers = answers
    # A JSON column mutated in place is invisible to the session's change
    # detection, and a silently unsaved answer is exactly the failure this
    # route exists to end.
    flag_modified(progress, "quiz_answers")

    db.commit()

    return record
