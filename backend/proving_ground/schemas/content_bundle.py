# backend/proving_ground/schemas/content_bundle.py
"""Schemas for the git-native content bundle (PG-104)."""

from typing import Any, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class BundleConflict(BaseModel):
    """One field where the library and the bundle disagree."""

    field: str
    local: Any = None
    incoming: Any = None
    summary: str = ""
    description: str = Field(description="One-line human-readable form")


class BundleDecision(BaseModel):
    """What importing a bundle would do, and why."""

    slug: str
    title: str
    action: str = Field(description="create | update | unchanged | conflict")
    content_id: Optional[UUID] = None
    conflicts: List[BundleConflict] = []
    reason: str
    writes: bool = Field(description="Whether carrying this out changes anything")


class BundleImportResult(BaseModel):
    decision: BundleDecision
    content_id: Optional[UUID] = None
    imported: bool = False
