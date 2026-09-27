# backend/proving_ground/schemas/catalog_contribution.py
"""Schemas for contributing a locally-modified blueprint back to its catalog."""

from typing import Any, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class BlueprintFieldChange(BaseModel):
    """One difference between the catalog item and the local blueprint."""

    key: str = Field(description="Stable identifier; send this back to select the change")
    path: List[str] = Field(description="Location in blueprint.yaml, segment by segment")
    kind: str = Field(description="changed | added | removed")
    label: str
    before: Any = None
    after: Any = None


class BlueprintCatalogOrigin(BaseModel):
    """The catalog item a blueprint was installed from."""

    source_id: UUID
    source_name: str
    source_url: str
    source_branch: Optional[str] = None
    item_id: str
    item_name: str
    installed_version: str
    item_path: str


class BlueprintCatalogDiff(BaseModel):
    origin: BlueprintCatalogOrigin
    changes: List[BlueprintFieldChange]
    notes: List[str] = Field(
        default_factory=list,
        description="Local changes that cannot be expressed in blueprint.yaml",
    )
    has_changes: bool


class BlueprintContributionRequest(BaseModel):
    changes: Optional[List[str]] = Field(
        default=None,
        description="Change keys to include; omit to include every change",
    )


class BlueprintContribution(BaseModel):
    origin: BlueprintCatalogOrigin
    patch: str
    blueprint_yaml: str
    applied: List[str]
    notes: List[str] = Field(default_factory=list)
    applies_to_source: bool = Field(
        description="False when the patch will not apply cleanly with `git apply`"
    )
    suggested_filename: str
