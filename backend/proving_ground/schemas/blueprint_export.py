# backend/proving_ground/schemas/blueprint_export.py
"""
Schemas for blueprint export/import functionality.

Version History:
- 1.0: Original export format with templates
- 2.0: Image Library IDs (templates deprecated)
- 3.0: Includes Dockerfiles and Content Library items
- 4.0: Unified Range Blueprints (consolidates Range Export + Blueprint Export)
       Adds: include_msel, include_artifacts options
- 5.0: Era B (Kubernetes) blueprints. `blueprint.config` is the stored document as written --
       workloads, capability packages with their scopes and values, content ids -- instead of an
       Era A model that has no field for any of them.

A package declares the oldest format that can describe it, not the newest this engine can write.
An Era A blueprint still leaves as 4.0 because nothing about it needs 5.0 to be understood, and
stamping it 5.0 would strand it on every install that has not upgraded yet. A bug travels between
the platform author and the content author as a blueprint export, so a package that an older host
refuses to read is a bug report that does not arrive.
"""

from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field, field_validator

# The format a package declares, by the era of the blueprint inside it.
PACKAGE_FORMAT_LEGACY = "4.0"
PACKAGE_FORMAT_KUBERNETES = "5.0"

# Every format this engine can read. A package outside this set is refused by name rather than
# half-imported: a newer writer declares fields this reader has no code for, and dropping them
# silently creates a blueprint that is missing whatever they said.
SUPPORTED_PACKAGE_FORMATS = ("1.0", "2.0", "3.0", "4.0", "5.0")


# ============ Template Export Schema (Deprecated) ============


class TemplateExportData(BaseModel):
    """Template data for export (deprecated - kept for backward compatibility)."""

    name: str
    description: Optional[str] = None
    os_type: str  # OSType enum value
    os_variant: Optional[str] = None
    base_image: Optional[str] = None
    vm_type: str  # VMType enum value
    linux_distro: Optional[str] = None
    boot_mode: Optional[str] = None
    disk_type: Optional[str] = None
    default_cpu: int = 1
    default_ram_mb: int = 1024
    default_disk_gb: int = 20
    config_script: Optional[str] = None
    tags: List[str] = []


# ============ Dockerfile Export Schema (v3.0) ============


class DockerfileProjectData(BaseModel):
    """Dockerfile project data for export. Era A only -- see BlueprintExportOptions."""

    project_name: str  # Directory name: e.g., "kali-attack"
    image_tag: str  # Full image tag: e.g., "proving_ground/kali-attack:latest"
    files: Dict[str, str]  # {filename: content} - Dockerfile, scripts, etc.
    description: Optional[str] = None


# ============ Content Library Export Schema (v3.0) ============


class ContentAssetExportData(BaseModel):
    """Content asset data for export (images, files)."""

    filename: str
    mime_type: str
    sha256_hash: str
    archive_path: str  # Path within the ZIP archive


class ContentExportData(BaseModel):
    """Content Library item data for export."""

    title: str
    content_type: str  # "student_guide", "instructor_guide", etc.
    body_markdown: str
    walkthrough_data: Optional[Dict[str, Any]] = None  # Structured walkthrough content
    content_hash: str  # SHA256 hash of body_markdown for deduplication
    assets: List[ContentAssetExportData] = []


# ============ Artifact Export Schema (v4.0) ============


class ArtifactExportData(BaseModel):
    """Artifact data for export."""

    name: str
    description: Optional[str] = None
    category: str  # "tool", "script", "evidence_template", etc.
    sha256_hash: str
    file_size: int
    archive_path: str  # Path within the ZIP archive


# ============ Export Options Schema (v4.0) ============


class BlueprintExportOptions(BaseModel):
    """Options controlling what to include in the export (v4.0)."""

    include_msel: bool = Field(
        default=True, description="Include MSEL (Master Scenario Events List) injects"
    )
    # Era A only. A Kubernetes range's images are digest-pinned references the cluster already
    # holds (ADR-0007); there is no /data/images to read and no daemon to read it with, so the
    # export service ignores both of these for a v2 blueprint rather than failing on them.
    include_dockerfiles: bool = Field(
        default=True, description="Era A only. Dockerfiles from /data/images/ for referenced images"
    )
    include_docker_images: bool = Field(
        default=False,
        description="Era A only. Image tarballs (large, but enables fully offline deployment)",
    )
    include_content: bool = Field(
        default=True, description="Include Content Library items (student guides, etc.)"
    )
    # Collected from `config["artifact_ids"]`. Nothing in the UI writes that key today, so a
    # blueprint that does not declare it exports no artifacts however this is set.
    include_artifacts: bool = Field(
        default=False, description="Include artifact files (tools, scripts, evidence templates)"
    )


# ============ Export Schemas ============


class BlueprintExportManifest(BaseModel):
    """Manifest for blueprint export package."""

    version: str = PACKAGE_FORMAT_LEGACY
    export_type: str = "blueprint"
    created_at: datetime
    created_by: Optional[str] = None
    proving_ground_version: Optional[str] = None  # The version that created this export
    blueprint_name: str
    # Which era the blueprint inside describes: 1 = networks and VMs, 2 = workloads and
    # capability packages. Stated here so a reader knows what it is holding without parsing the
    # config, and so an import summary can name machines the way that era does.
    blueprint_schema_version: int = 1
    workload_count: int = 0
    capability_count: int = 0
    # What's included (v4.0)
    msel_included: bool = False
    dockerfile_count: int = 0
    content_included: bool = False
    artifact_count: int = 0
    docker_images_included: bool = False
    docker_image_count: int = 0  # Number of image tarballs
    docker_images: List[str] = []  # List of image tags exported
    # Checksums for integrity verification
    checksums: Dict[str, str] = {}


class BlueprintExportData(BaseModel):
    """Blueprint data for export."""

    name: str
    description: Optional[str] = None
    version: int = 1
    # The stored document, as written, for the same reason `BlueprintDetailResponse.config` is:
    # typing this as the Era A model means a v2 config either fails validation outright or is
    # accepted with its workloads and capabilities dropped on the way into the package. A
    # discriminated union cannot replace it either -- a v1 config carries no schemaVersion at
    # all, and absence is what makes it v1.
    config: Dict[str, Any]
    student_guide_id: Optional[str] = None  # Content Library ID (if linked)

    @field_validator("config", mode="before")
    @classmethod
    def _as_document(cls, value: Any) -> Any:
        """Accept a `BlueprintConfig` as well as the raw document.

        The async export task still builds this from the Era A model, and it is not worth a
        broken export path to make it stop.
        """
        return value.model_dump() if isinstance(value, BaseModel) else value


class BlueprintExportFull(BaseModel):
    """Full blueprint export package structure."""

    manifest: BlueprintExportManifest
    blueprint: BlueprintExportData
    templates: List[TemplateExportData] = []  # Deprecated - kept for backward compatibility
    dockerfiles: List[DockerfileProjectData] = []  # Dockerfile projects (Era A)
    content: Optional[ContentExportData] = None  # Student guide / content
    artifacts: List[ArtifactExportData] = []  # Artifact files (v4.0)


# ============ Import Schemas ============


class BlueprintImportValidation(BaseModel):
    """Validation result for blueprint import."""

    valid: bool
    blueprint_name: str
    manifest_version: str = "1.0"
    errors: List[str] = []
    warnings: List[str] = []
    conflicts: List[str] = []
    missing_templates: List[str] = []  # Deprecated
    included_templates: List[str] = []  # Deprecated
    included_dockerfiles: List[str] = []
    dockerfile_conflicts: List[str] = []
    missing_images: List[str] = []
    content_included: bool = False
    content_conflict: Optional[str] = None
    # v4.0 additions
    msel_included: bool = False
    included_artifacts: List[str] = []
    artifact_conflicts: List[str] = []
    # v5.0 additions: what a Kubernetes blueprint contains, so the review step describes the
    # package in the words of the era that wrote it instead of reporting it as empty.
    blueprint_schema_version: int = 1
    included_networks: List[str] = []
    included_workloads: List[str] = []
    # "<name> (<scope>)" -- scope is required on every capability with no default (ADR-0004), so
    # it is shown beside the name rather than left for the importer to discover at deploy time.
    included_capabilities: List[str] = []


class BlueprintImportOptions(BaseModel):
    """Options for blueprint import."""

    template_conflict_strategy: str = Field(
        default="skip",
        description="How to handle template conflicts: skip, update, or error (deprecated)",
    )
    new_name: Optional[str] = Field(
        default=None, description="Rename blueprint on import to avoid name conflicts"
    )
    dockerfile_conflict_strategy: str = Field(
        default="skip",
        description="How to handle Dockerfile conflicts: skip (use existing), overwrite, or error",
    )
    content_conflict_strategy: str = Field(
        default="skip",
        description="How to handle Content Library conflicts: skip (use existing), rename, use_existing",
    )
    # Era A only, and refused rather than attempted elsewhere: building an image needs a Docker
    # daemon, and a Kubernetes install has none for the worker to reach.
    build_images: bool = Field(
        default=True, description="Era A only. Build images from included Dockerfiles"
    )


class BlueprintImportResult(BaseModel):
    """Result of blueprint import operation."""

    success: bool
    blueprint_id: Optional[UUID] = None
    blueprint_name: Optional[str] = None
    templates_created: List[str] = []  # Deprecated
    templates_skipped: List[str] = []  # Deprecated
    images_built: List[str] = []
    images_loaded: List[str] = []  # Images loaded from tar and pushed to registry
    images_skipped: List[str] = []  # Images already in registry (skipped)
    dockerfiles_extracted: List[str] = []
    dockerfiles_skipped: List[str] = []
    content_imported: bool = False
    content_id: Optional[UUID] = None
    # v4.0 additions
    artifacts_imported: List[str] = []
    artifacts_skipped: List[str] = []
    # v5.0 addition: what the created blueprint turned out to be, so a caller does not have to
    # re-read it to find out whether it can deploy here.
    blueprint_schema_version: int = 1
    errors: List[str] = []
    warnings: List[str] = []
