# backend/proving_ground/schemas/content.py
"""Pydantic schemas for Content API."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from proving_ground.models.content import ContentType

# ============ Walkthrough Schemas ============
#
# There are two shapes for a walkthrough here, and the difference is the point.
# The authoring shape below carries the answer key, because an author writing a
# Knowledge Check has to say which option is right. The learner shape further
# down does not carry it at all, because the browser is the one party to an
# assessment whose copy of the answers cannot be trusted.


class QuizOptionSchema(BaseModel):
    """One selectable answer within a quiz question, as its author writes it."""

    id: str
    text: str
    correct: bool = False


class QuizQuestionSchema(BaseModel):
    """A single multiple-choice question with immediate feedback."""

    id: str
    prompt: str
    options: List[QuizOptionSchema] = Field(default_factory=list)
    explanation: Optional[str] = None


class WalkthroughStepSchema(BaseModel):
    """A single step in a walkthrough phase."""

    id: str
    title: str
    content: str
    vm: Optional[str] = None
    hints: Optional[List[str]] = None
    # Optional interactive knowledge check rendered after the step content.
    quiz: Optional[List[QuizQuestionSchema]] = None


class WalkthroughPhaseSchema(BaseModel):
    """A phase containing multiple steps."""

    id: str
    name: str
    steps: List[WalkthroughStepSchema] = Field(default_factory=list)


class WalkthroughSchema(BaseModel):
    """Complete walkthrough structure."""

    title: str
    phases: List[WalkthroughPhaseSchema] = Field(default_factory=list)


# ============ Learner-facing Walkthrough Schemas ============
#
# What a range hands to a browser. Every field below is optional or defaulted
# and the containers accept unknown keys, because this is a read path: content
# authored before a field existed must still render rather than fail
# validation. The quiz schemas are the exception -- they accept no extras, so
# no answer key can arrive through one.


class QuizAnswerRecordSchema(BaseModel):
    """One graded answer, as held in the learner record and returned on submit.

    The answer key lives here and nowhere else in a learner response: by the
    time this exists the learner has already answered, so telling them which
    option was right reveals nothing they can still use.
    """

    question_id: str
    step_id: Optional[str] = None
    selected_option_id: str
    correct: bool
    correct_option_id: Optional[str] = None
    explanation: Optional[str] = None
    answered_at: Optional[str] = None


class LearnerQuizOptionSchema(BaseModel):
    """A selectable answer as the learner receives it.

    It has no `correct` field, and that absence is the fix: every option used
    to arrive carrying its own flag, so the key to the Knowledge Check was
    readable in the page source before the learner picked anything.
    """

    id: str = ""
    text: str = ""


class LearnerQuizQuestionSchema(BaseModel):
    """A question as the learner receives it.

    No `explanation` either -- an explanation that says why an option is right
    is the answer key in prose.
    """

    id: str = ""
    prompt: str = ""
    options: List[LearnerQuizOptionSchema] = Field(default_factory=list)
    answered: Optional[QuizAnswerRecordSchema] = None


class LearnerWalkthroughStepSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = ""
    title: str = ""
    content: str = ""
    vm: Optional[str] = None
    hints: Optional[List[str]] = None
    quiz: Optional[List[LearnerQuizQuestionSchema]] = None


class LearnerWalkthroughPhaseSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = ""
    name: str = ""
    steps: List[LearnerWalkthroughStepSchema] = Field(default_factory=list)


class LearnerWalkthroughSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    title: str = ""
    phases: List[LearnerWalkthroughPhaseSchema] = Field(default_factory=list)


# ============ Content Asset Schemas ============


class ContentAssetBase(BaseModel):
    filename: str
    mime_type: str
    file_size: int = 0


class ContentAssetCreate(ContentAssetBase):
    pass


class ContentAssetResponse(ContentAssetBase):
    id: UUID
    content_id: UUID
    file_path: str
    sha256_hash: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ============ Content Schemas ============


class ContentBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    content_type: ContentType = ContentType.CUSTOM
    tags: List[str] = Field(default_factory=list)


class ContentCreate(ContentBase):
    body_markdown: str = ""
    walkthrough_data: Optional[WalkthroughSchema] = None
    organization: Optional[str] = None


class ContentUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    content_type: Optional[ContentType] = None
    body_markdown: Optional[str] = None
    walkthrough_data: Optional[WalkthroughSchema] = None
    tags: Optional[List[str]] = None
    organization: Optional[str] = None
    is_published: Optional[bool] = None


class ContentResponse(ContentBase):
    id: UUID
    body_markdown: str
    body_html: Optional[str] = None
    walkthrough_data: Optional[Dict[str, Any]] = None
    version: str
    is_published: bool
    organization: Optional[str] = None
    created_by_id: UUID
    created_at: datetime
    updated_at: datetime
    assets: List[ContentAssetResponse] = Field(default_factory=list)

    class Config:
        from_attributes = True


class ContentListResponse(BaseModel):
    id: UUID
    title: str
    description: Optional[str] = None
    content_type: ContentType
    version: str
    tags: List[str]
    is_published: bool
    created_by_id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ContentExport(BaseModel):
    """Content export format."""

    title: str
    description: Optional[str] = None
    content_type: ContentType
    body_markdown: str
    version: str
    tags: List[str]
    organization: Optional[str] = None
    exported_at: datetime
    export_format: str = "json"


class ContentImport(BaseModel):
    """Content import format."""

    title: str
    description: Optional[str] = None
    content_type: ContentType = ContentType.CUSTOM
    body_markdown: str
    version: Optional[str] = "1.0"
    tags: List[str] = Field(default_factory=list)
    organization: Optional[str] = None
