# backend/proving_ground/schemas/catalog_install.py
"""Schemas for one-click catalog install with dependency resolution (PG-149)."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class InstallStep(BaseModel):
    """One unit of work in an install plan."""

    key: str = Field(description="Stable identifier, e.g. base_image/ubuntu-22-04")
    kind: str = Field(description="base_image | image | content | blueprint")
    ref: str
    name: str
    label: str = Field(description="What the progress log says while this runs")
    satisfied: bool = Field(description="Already installed; will be skipped")
    available: bool = Field(description="False when the catalog does not contain it")
    note: str = ""


class InstallPlanResponse(BaseModel):
    item_id: str
    steps: List[InstallStep]
    total_steps: int = Field(description="Steps that will actually run")
    warnings: List[str] = []
    summary: str


class InstallJobStarted(BaseModel):
    job_id: str
    status: str
    total_steps: int
    message: str


class InstallLogLine(BaseModel):
    message: str
    level: str = "info"
    at: Optional[str] = None


class InstallJobStatus(BaseModel):
    status: str = Field(description="pending | running | completed | failed | cancelled")
    step: str = ""
    progress: int = 0
    total_steps: int = 0
    current_item: str = ""
    error: str = ""
    result: Optional[Dict[str, Any]] = None
    log: List[InstallLogLine] = []
    updated_at: Optional[str] = None
