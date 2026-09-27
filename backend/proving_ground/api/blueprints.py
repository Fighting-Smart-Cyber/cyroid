# backend/proving_ground/api/blueprints.py
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple
from uuid import UUID
from fastapi import APIRouter, HTTPException, status, UploadFile, File, Query
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from proving_ground.api import kubernetes_ranges
from proving_ground.api.deps import DBSession, CurrentUser, DownloadUser
from proving_ground.capability.blueprint import SCHEMA_VERSION_LEGACY, RangeSpec, read_blueprint
from proving_ground.models import Range, RangeBlueprint, RangeInstance
from proving_ground.models.range import RangeStatus
from proving_ground.models.catalog import CatalogInstalledItem
from proving_ground.schemas.blueprint import (
    BlueprintCreate,
    BlueprintUpdate,
    BlueprintResponse,
    BlueprintDetailResponse,
    InstanceDeploy,
    InstanceResponse,
    BlueprintConfig,
)
from proving_ground.schemas.catalog_contribution import (
    BlueprintCatalogDiff,
    BlueprintCatalogOrigin,
    BlueprintContribution,
    BlueprintContributionRequest,
    BlueprintFieldChange,
)
from proving_ground.schemas.blueprint_export import (
    BlueprintImportValidation,
    BlueprintImportOptions,
    BlueprintImportResult,
    BlueprintExportOptions,
)
from proving_ground.services.blueprint_service import (
    extract_config_from_range,
    create_range_from_blueprint,
)
from proving_ground.services.blueprint_export_service import get_blueprint_export_service
from proving_ground.tasks.deployment import deploy_range_task

router = APIRouter(prefix="/blueprints", tags=["blueprints"])

logger = logging.getLogger(__name__)


def _read(config: Optional[Mapping[str, Any]]) -> RangeSpec:
    """Read a config the caller supplied, turning the reader's own refusal into a 422.

    `read_blueprint` is the one authority on what a blueprint config means in either era, and its
    messages name the workload, the capability and the field. A generic "invalid blueprint" would
    throw that away, and a config is hand-edited as often as it is generated.
    """
    try:
        return read_blueprint(config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TypeError as exc:
        # The reader coerces the fields it reads -- `int(entry["cpus"])` -- so a config that puts
        # an object where a number belongs raises TypeError rather than ValueError. It is still a
        # config somebody wrote, and a bare 500 would blame the server for it.
        raise HTTPException(
            status_code=422, detail=f"blueprint config has a field of the wrong type: {exc}"
        ) from exc


def _config_to_store(config: Mapping[str, Any]) -> dict:
    """Validate an incoming config and return the document to persist.

    A v2 config is stored as written: `BlueprintConfig` describes Era A and nothing else, and
    forcing a Kubernetes config through it is what replaced an install's workloads and
    capabilities with empty lists. A v1 config still goes through `BlueprintConfig`, because the
    Era A deploy path reads it back as that model and depends on its defaults -- but the keys
    that model does not know are kept rather than dropped, capability packages among them, which
    `read_blueprint` reads from either era.
    """
    spec = _read(config)
    if spec.deployable_on_kubernetes:
        return dict(config)
    try:
        legacy = BlueprintConfig.model_validate(config)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**config, **legacy.model_dump()}


def _is_kubernetes_config(config: Optional[Mapping[str, Any]]) -> bool:
    """Whether a stored config describes a Kubernetes range, without ever raising.

    A config too malformed for `read_blueprint` still has an era -- the one it declares -- and on
    the guard paths below that answer matters more than deployability: a broken v2 blueprint can
    be repaired, an overwritten one cannot.
    """
    config = config or {}
    try:
        return read_blueprint(config).deployable_on_kubernetes
    except ValueError:
        return config.get("schemaVersion", SCHEMA_VERSION_LEGACY) != SCHEMA_VERSION_LEGACY


def _is_kubernetes_range(db: Session, range_id: UUID) -> bool:
    """Whether this range's machines live on the cluster rather than in Network and VM rows.

    Either the host deploys ranges on Kubernetes, or the range was instantiated from a v2
    blueprint. `extract_config_from_range` describes neither: it reads Network, VM and VMNetwork
    rows, so against such a range it returns an empty v1 config rather than failing.
    """
    if kubernetes_ranges.is_kubernetes():
        return True
    instance = db.query(RangeInstance).filter(RangeInstance.range_id == range_id).first()
    if instance is None or instance.blueprint is None:
        return False
    return _is_kubernetes_config(instance.blueprint.config)


def _require_blueprint_control(blueprint: RangeBlueprint, current_user) -> None:
    """Admin, or the person who created it. A blueprint with no creator needs an admin.

    Three routes used to spell this inline and each spelled it slightly differently: the config
    branch of PUT checked it, PUT's metadata branch did not, and DELETE had no check at all -- so
    any authenticated account could rename or delete any blueprint. All three also guarded on
    `if blueprint.created_by and ...`, which reads as "an unowned blueprint is everyone's":
    a seeded blueprint has `created_by` NULL, and on a Kubernetes install the seeded one is the
    only blueprint that can be deployed at all.
    """
    # `current_user.is_admin`, not `any(role.name == "admin" for role in current_user.roles)`:
    # `User.roles` is a list of strings, so the old spelling compared a string's `.name` and
    # raised -- caught nowhere, because the branch was only reached for a blueprint somebody
    # else owned. The admin bypass these routes claimed to have has never worked.
    if current_user.is_admin:
        return
    if blueprint.created_by and blueprint.created_by == current_user.id:
        return
    raise HTTPException(status_code=403, detail="Not authorized to modify this blueprint")


def _schema_version(config: Optional[Mapping[str, Any]]) -> int:
    """2 for a Kubernetes blueprint, 1 for an Era A one -- what a card labels its count from."""
    return 2 if _is_kubernetes_config(config or {}) else 1


def _counts(config: Optional[Mapping[str, Any]]) -> Tuple[int, int]:
    """Networks and machines for a blueprint card, whichever era wrote the config.

    A v2 blueprint's machines are its workloads and it has no `vms` key at all, so counting `vms`
    reports every Kubernetes blueprint as empty.
    """
    config = config or {}
    networks = config.get("networks")
    machines = config.get("workloads" if _is_kubernetes_config(config) else "vms")
    return (
        len(networks) if isinstance(networks, list) else 0,
        len(machines) if isinstance(machines, list) else 0,
    )


@router.post("", response_model=BlueprintDetailResponse, status_code=status.HTTP_201_CREATED)
def create_blueprint(data: BlueprintCreate, db: DBSession, current_user: CurrentUser):
    """Create a new blueprint, from an existing range or from a config supplied directly."""
    if data.config is not None:
        config = _config_to_store(data.config)
    else:
        # Verify range exists
        range_obj = db.query(Range).filter(Range.id == data.range_id).first()
        if not range_obj:
            raise HTTPException(status_code=404, detail="Range not found")

        if _is_kubernetes_range(db, data.range_id):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Saving a range as a blueprint reads its networks and VM rows, which a range "
                    "on the Kubernetes substrate does not have -- the blueprint would describe an "
                    "empty range that cannot be deployed. Author it as a v2 config instead."
                ),
            )

        # Extract config from range
        config = extract_config_from_range(db, data.range_id).model_dump()

    # Create blueprint
    # Note: base_subnet_prefix and next_offset are deprecated with DinD isolation
    blueprint = RangeBlueprint(
        name=data.name,
        description=data.description,
        config=config,
        base_subnet_prefix=data.base_subnet_prefix,  # Optional, kept for backward compatibility
        created_by=current_user.id,
        version=1,
        next_offset=0,
    )
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)

    return _blueprint_to_detail_response(blueprint, blueprint.config, current_user.username)


@router.get("", response_model=List[BlueprintResponse])
def list_blueprints(db: DBSession, current_user: CurrentUser):
    """List all blueprints."""
    blueprints = db.query(RangeBlueprint).all()
    return [_blueprint_to_response(b, db) for b in blueprints]


@router.get("/{blueprint_id}", response_model=BlueprintDetailResponse)
def get_blueprint(blueprint_id: UUID, db: DBSession, current_user: CurrentUser):
    """Get blueprint details."""
    blueprint = db.query(RangeBlueprint).filter(RangeBlueprint.id == blueprint_id).first()
    if not blueprint:
        raise HTTPException(status_code=404, detail="Blueprint not found")

    # Get creator username
    from proving_ground.models import User

    creator = db.query(User).filter(User.id == blueprint.created_by).first()
    username = creator.username if creator else None

    return _blueprint_to_detail_response(blueprint, blueprint.config, username)


@router.put("/{blueprint_id}", response_model=BlueprintDetailResponse)
def update_blueprint(
    blueprint_id: UUID, data: BlueprintUpdate, db: DBSession, current_user: CurrentUser
):
    """Update blueprint metadata and/or config. Increments version if config changes."""
    from proving_ground.models import User

    blueprint = db.query(RangeBlueprint).filter(RangeBlueprint.id == blueprint_id).first()
    if not blueprint:
        raise HTTPException(status_code=404, detail="Blueprint not found")

    _require_blueprint_control(blueprint, current_user)

    # Read the config before anything is written, so a config the reader refuses is a 422 and
    # not a saved edit followed by a failed response.
    new_config = _config_to_store(data.config) if data.config is not None else None

    if data.name is not None:
        blueprint.name = data.name
    if data.description is not None:
        blueprint.description = data.description
    if data.content_ids is not None:
        blueprint.content_ids = data.content_ids

    # Update config and increment version
    if new_config is not None:
        blueprint.config = new_config
        blueprint.version += 1

    db.commit()
    db.refresh(blueprint)

    # Get creator username for response
    creator = db.query(User).filter(User.id == blueprint.created_by).first()
    username = creator.username if creator else None

    return _blueprint_to_detail_response(blueprint, blueprint.config, username)


@router.put("/{blueprint_id}/update-from-range/{range_id}", response_model=BlueprintDetailResponse)
def update_blueprint_from_range(
    blueprint_id: UUID,
    range_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
) -> BlueprintDetailResponse:
    """Update blueprint config from a modified range instance. Increments version."""
    from proving_ground.models import User

    # Verify blueprint exists
    blueprint = db.query(RangeBlueprint).filter(RangeBlueprint.id == blueprint_id).first()
    if not blueprint:
        raise HTTPException(status_code=404, detail="Blueprint not found")

    _require_blueprint_control(blueprint, current_user)

    # Verify range is an instance of this blueprint
    instance = (
        db.query(RangeInstance)
        .filter(
            RangeInstance.blueprint_id == blueprint_id,
            RangeInstance.range_id == range_id,
        )
        .first()
    )
    if not instance:
        raise HTTPException(status_code=404, detail="Range is not an instance of this blueprint")

    # `extract_config_from_range` builds its answer out of Network, VM and VMNetwork rows, which
    # is an Era A range and nothing else. Run against a Kubernetes range it returns an empty v1
    # config -- and writing that back replaces the blueprint's workloads and capability packages
    # with empty lists, bumps the version and reports success. One click, and the only blueprint
    # a Kubernetes install has describes nothing.
    if _is_kubernetes_config(blueprint.config):
        raise HTTPException(
            status_code=409,
            detail=(
                "This blueprint describes a Kubernetes range (schema v2), and updating a "
                "blueprint from a range reads networks and VM rows that such a range does not "
                "have. It would replace the blueprint's workloads and capabilities with nothing. "
                "Edit the blueprint's config instead."
            ),
        )
    if _is_kubernetes_range(db, range_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "This range is deployed on the Kubernetes substrate, whose networks and machines "
                "live on the cluster rather than in this platform's rows. There is nothing here "
                "to extract, so the blueprint would be emptied rather than updated."
            ),
        )

    # Extract new config from range
    new_config = extract_config_from_range(db, range_id)

    # Merged over the stored document rather than written in its place. Extraction answers out of
    # Network and VM rows, and a capability package has none of those -- it is declared in the
    # config and read from a v1 blueprint too. Replacing the document outright dropped every
    # capability the blueprint declared, silently, with the version bumped and a green toast.
    blueprint.config = {**(blueprint.config or {}), **new_config.model_dump()}
    blueprint.version += 1

    db.commit()
    db.refresh(blueprint)

    # Get creator username for response
    creator = db.query(User).filter(User.id == blueprint.created_by).first()
    username = creator.username if creator else None

    return _blueprint_to_detail_response(blueprint, blueprint.config, username)


@router.delete("/{blueprint_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_blueprint(blueprint_id: UUID, db: DBSession, current_user: CurrentUser):
    """Delete a blueprint and its associated content. Fails if instances exist."""
    from proving_ground.models.content import Content

    blueprint = db.query(RangeBlueprint).filter(RangeBlueprint.id == blueprint_id).first()
    if not blueprint:
        raise HTTPException(status_code=404, detail="Blueprint not found")

    _require_blueprint_control(blueprint, current_user)

    # Check for instances
    instance_count = (
        db.query(RangeInstance).filter(RangeInstance.blueprint_id == blueprint_id).count()
    )
    if instance_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete blueprint with {instance_count} active instances",
        )

    # Delete associated content (static content is owned by the blueprint)
    if blueprint.content_ids:
        for content_id_str in blueprint.content_ids:
            try:
                content_id = UUID(content_id_str)
                content = db.query(Content).filter(Content.id == content_id).first()
                if content:
                    # Clean up catalog installed item for content before deleting
                    db.query(CatalogInstalledItem).filter(
                        CatalogInstalledItem.local_resource_id == content_id
                    ).delete()
                    db.delete(content)
            except (ValueError, TypeError):
                pass  # Skip invalid UUIDs

    # Clean up catalog installed item record if this blueprint was installed from catalog
    db.query(CatalogInstalledItem).filter(
        CatalogInstalledItem.local_resource_id == blueprint_id
    ).delete()

    db.delete(blueprint)
    db.commit()


@router.post(
    "/{blueprint_id}/deploy", response_model=InstanceResponse, status_code=status.HTTP_201_CREATED
)
def deploy_instance(
    blueprint_id: UUID, data: InstanceDeploy, db: DBSession, current_user: CurrentUser
):
    """Deploy a new instance from a blueprint."""
    blueprint = db.query(RangeBlueprint).filter(RangeBlueprint.id == blueprint_id).first()
    if not blueprint:
        raise HTTPException(status_code=404, detail="Blueprint not found")

    # Get next offset and increment
    offset = blueprint.next_offset
    blueprint.next_offset += 1

    if _read(blueprint.config).deployable_on_kubernetes:
        # An Era B blueprint (PG-122): networks and workloads are realised on the cluster from
        # the config at deploy time, not stored as Network and VM rows. The range row is the
        # instance's handle and nothing more.
        range_obj = Range(
            name=data.name,
            description=f"Instance of blueprint '{blueprint.name}' (Kubernetes substrate)",
            created_by=current_user.id,
            status=RangeStatus.DRAFT,
        )
        db.add(range_obj)
        db.flush()
    else:
        config = BlueprintConfig.model_validate(blueprint.config)

        # Include linked content from blueprint model (saved separately from config JSON)
        if blueprint.content_ids:
            config.content_ids = blueprint.content_ids

        # Create range from blueprint with offset
        range_obj = create_range_from_blueprint(
            db=db,
            config=config,
            range_name=data.name,
            base_prefix=blueprint.base_subnet_prefix,
            offset=offset,
            created_by=current_user.id,
        )

    # Create instance record
    instance = RangeInstance(
        name=data.name,
        blueprint_id=blueprint.id,
        blueprint_version=blueprint.version,
        subnet_offset=offset,
        instructor_id=current_user.id,
        range_id=range_obj.id,
    )
    db.add(instance)

    # Flushed, not committed, so the refusal below can undo the whole thing. `validate_for_deploy`
    # reads the instance back through this session to find the blueprint, and a flushed row is
    # visible to a query in the same transaction -- but an uncommitted one leaves nothing behind
    # if the deploy is refused. Committing first and then refusing is how a 400 used to strand a
    # range and an instance the user never got.
    db.flush()

    if data.auto_deploy and kubernetes_ranges.is_kubernetes():
        # The same check `POST /ranges/{id}/deploy` already makes, made here too. This endpoint
        # answered 201 the moment the task was enqueued and then let the worker discover the
        # refusal, which reached the user as a range stuck on "deploying" and an explanation in
        # an event log nobody is looking at.
        try:
            kubernetes_ranges.validate_for_deploy(db, range_obj.id)
        except HTTPException:
            db.rollback()
            raise

    db.commit()
    db.refresh(instance)

    # Auto-deploy if requested (queue async task)
    if data.auto_deploy:
        deploy_range_task.send(str(range_obj.id))

    return _instance_to_response(instance, db)


@router.get("/{blueprint_id}/instances", response_model=List[InstanceResponse])
def list_instances(blueprint_id: UUID, db: DBSession, current_user: CurrentUser):
    """List all instances of a blueprint."""
    blueprint = db.query(RangeBlueprint).filter(RangeBlueprint.id == blueprint_id).first()
    if not blueprint:
        raise HTTPException(status_code=404, detail="Blueprint not found")

    instances = db.query(RangeInstance).filter(RangeInstance.blueprint_id == blueprint_id).all()

    return [_instance_to_response(i, db) for i in instances]


# ============ Export/Import Endpoints ============


def _blueprint_for_export(blueprint_id: UUID, db: Session) -> RangeBlueprint:
    """The blueprint, or a 404 that means what it says.

    Both export routes turned every `ValueError` the export service could raise into "not found",
    so a blueprint that was sitting in front of the user reported itself missing and sent them
    hunting for it. Asking the question here leaves 404 meaning only a genuine miss.
    """
    blueprint = db.query(RangeBlueprint).filter(RangeBlueprint.id == blueprint_id).first()
    if not blueprint:
        raise HTTPException(status_code=404, detail="Blueprint not found")
    return blueprint


def _export_refusal(blueprint: RangeBlueprint, exc: Exception, *, action: str) -> HTTPException:
    """Refuse without quoting the blueprint's own config back to the caller.

    Pydantic renders each validation error with a repr of the input value, and the input value
    here is the stored document -- hook commands, chart values, whatever the author put in it.
    `ValidationError` is a subclass of `ValueError`, so the handler that interpolated `str(e)`
    into the response body shipped a fragment of that document to anyone who could reach the
    endpoint. The detail belongs in the log, where an operator can read it; the caller gets the
    part they can act on, which is which blueprint and what kind of failure it was.
    """
    logger.warning("blueprint %s could not be %s", blueprint.id, action, exc_info=exc)
    if isinstance(exc, ValidationError):
        return HTTPException(
            status_code=409,
            detail=(
                f"This blueprint's stored configuration (schema v"
                f"{_schema_version(blueprint.config)}) could not be read, so it cannot be "
                f"{action}. The validation detail is in the platform's log."
            ),
        )
    return HTTPException(
        status_code=500,
        detail=f"This blueprint could not be {action}. The reason is in the platform's log.",
    )


@router.get("/{blueprint_id}/export-size")
def get_export_size(
    blueprint_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
    include_docker_images: bool = Query(
        default=False, description="Include Docker image tarballs in size estimate"
    ),
):
    """
    Get estimated export size for a blueprint.

    Returns size estimates for Docker images if requested.
    Useful for showing users expected download size before exporting.
    """
    export_service = get_blueprint_export_service()
    blueprint = _blueprint_for_export(blueprint_id, db)

    try:
        return export_service.estimate_export_size(
            blueprint_id=blueprint_id,
            db=db,
            include_docker_images=include_docker_images,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise _export_refusal(blueprint, e, action="measured") from e


@router.get("/{blueprint_id}/export")
def export_blueprint(
    blueprint_id: UUID,
    db: DBSession,
    current_user: DownloadUser,  # Uses token query param for browser downloads
    include_msel: bool = Query(
        default=True, description="Include MSEL (Master Scenario Events List) injects"
    ),
    include_dockerfiles: bool = Query(
        default=True, description="Include Dockerfiles from /data/images/ for referenced images"
    ),
    include_docker_images: bool = Query(
        default=False,
        description="Include Docker image tarballs (large, but enables fully offline deployment)",
    ),
    include_content: bool = Query(
        default=True, description="Include Content Library items (student guides, etc.)"
    ),
    include_artifacts: bool = Query(
        default=False, description="Include artifact files (tools, scripts, evidence templates)"
    ),
    content_id: str = Query(
        default=None, description="Specific Content Library ID to include (UUID)"
    ),
):
    """
    Export a blueprint as a portable ZIP package (v4.0 unified format).

    The package includes:
    - Blueprint configuration (networks, VMs)
    - MSEL injects (optional)
    - Dockerfiles from /data/images/ for referenced images (optional)
    - Content Library items (optional)
    - Artifact files (optional)
    - Manifest with checksums

    Options:
    - include_msel: Include MSEL injects
    - include_dockerfiles: Include Dockerfile projects for custom images
    - include_docker_images: Include Docker image tarballs (very large)
    - include_content: Include Content Library items
    - include_artifacts: Include artifact files
    - content_id: Specific Content ID to include
    """
    export_service = get_blueprint_export_service()
    blueprint = _blueprint_for_export(blueprint_id, db)

    try:
        options = BlueprintExportOptions(
            include_msel=include_msel,
            include_dockerfiles=include_dockerfiles,
            include_docker_images=include_docker_images,
            include_content=include_content,
            include_artifacts=include_artifacts,
        )

        # Parse content_id if provided
        parsed_content_id = None
        if content_id:
            try:
                parsed_content_id = UUID(content_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail="Invalid content_id UUID format"
                ) from exc

        archive_path, filename = export_service.export_blueprint(
            blueprint_id=blueprint_id,
            user=current_user,
            db=db,
            options=options,
            content_id=parsed_content_id,
        )

        return FileResponse(
            path=str(archive_path),
            filename=filename,
            media_type="application/zip",
            background=None,  # Don't delete file immediately
        )
    except HTTPException:
        # The bad-content_id 400 above is raised inside this try. Without this branch the
        # catch-all below swallowed it and answered 500, blaming the server for the caller's
        # query string.
        raise
    except Exception as e:
        raise _export_refusal(blueprint, e, action="exported") from e


# ============ Async Export Endpoints ============


@router.post("/{blueprint_id}/export/start")
def start_async_export(
    blueprint_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
    include_msel: bool = Query(default=True),
    include_dockerfiles: bool = Query(default=True),
    include_docker_images: bool = Query(default=False),
    include_content: bool = Query(default=True),
    include_artifacts: bool = Query(default=False),
):
    """
    Start an async blueprint export job.

    Returns a job_id that can be used to check status, cancel, or download.
    This is preferred for large exports (especially with Docker images).
    """
    import uuid
    from proving_ground.tasks.blueprint_export import export_blueprint_async, update_job_status

    # Verify blueprint exists
    blueprint = db.query(RangeBlueprint).filter(RangeBlueprint.id == blueprint_id).first()
    if not blueprint:
        raise HTTPException(status_code=404, detail="Blueprint not found")

    # Generate job ID
    job_id = str(uuid.uuid4())

    # Initialize job status
    update_job_status(job_id, "pending", "Queued for export...", 0, 6)

    # Build options dict
    options_dict = {
        "include_msel": include_msel,
        "include_dockerfiles": include_dockerfiles,
        "include_docker_images": include_docker_images,
        "include_content": include_content,
        "include_artifacts": include_artifacts,
    }

    # Queue the task
    export_blueprint_async.send(
        job_id,
        str(blueprint_id),
        str(current_user.id),
        options_dict,
    )

    return {
        "job_id": job_id,
        "status": "pending",
        "message": "Export job queued",
    }


@router.get("/export/{job_id}/status")
def get_export_status(job_id: str, current_user: CurrentUser):
    """
    Get the status of an async export job.

    Returns current step, progress, and any errors.
    """
    from proving_ground.tasks.blueprint_export import get_job_status

    status = get_job_status(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Export job not found")

    return status


@router.post("/export/{job_id}/cancel")
def cancel_export(job_id: str, current_user: CurrentUser):
    """
    Cancel an in-progress export job.

    This will stop the export and clean up any temporary files.
    """
    from proving_ground.tasks.blueprint_export import cancel_job, get_job_status

    status = get_job_status(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Export job not found")

    if status.get("status") not in ("pending", "running"):
        raise HTTPException(
            status_code=400, detail=f"Cannot cancel job with status: {status.get('status')}"
        )

    if cancel_job(job_id):
        return {"message": "Export cancelled", "job_id": job_id}
    else:
        raise HTTPException(status_code=400, detail="Failed to cancel export")


@router.get("/export/{job_id}/download")
def download_export(job_id: str, current_user: DownloadUser):
    """
    Download a completed export.

    The job must be in 'completed' status.
    After download, the job data and files are cleaned up.
    """
    from proving_ground.tasks.blueprint_export import get_job_status

    status = get_job_status(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Export job not found")

    if status.get("status") != "completed":
        raise HTTPException(
            status_code=400, detail=f"Export not ready. Status: {status.get('status')}"
        )

    download_path = status.get("download_path")
    filename = status.get("filename")

    if not download_path or not Path(download_path).exists():
        raise HTTPException(status_code=404, detail="Export file not found")

    # Return the file
    # Note: We don't clean up immediately to allow re-download
    # Cleanup happens via TTL in Redis or manual cleanup
    return FileResponse(
        path=download_path,
        filename=filename,
        media_type="application/zip",
    )


@router.post("/import/validate", response_model=BlueprintImportValidation)
async def validate_blueprint_import(
    file: UploadFile = File(...),
    db: DBSession = None,
    current_user: CurrentUser = None,
):
    """
    Validate a blueprint import package (dry-run) - v3.0.

    Checks for:
    - Blueprint name conflicts
    - Dockerfile project conflicts
    - Missing Docker images that need to be built
    - Content Library conflicts
    - VM image source availability
    """
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="File must be a ZIP archive")

    # Save uploaded file temporarily
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    try:
        content = await file.read()
        temp_file.write(content)
        temp_file.close()

        export_service = get_blueprint_export_service()
        result = export_service.validate_import(
            archive_path=temp_file.name,
            db=db,
        )
        return result
    finally:
        # Clean up temp file
        try:
            os.unlink(temp_file.name)
        except Exception:
            pass


@router.post("/import", response_model=BlueprintImportResult)
async def import_blueprint(
    file: UploadFile = File(...),
    template_conflict_strategy: str = Query(
        default="skip",
        description="(Deprecated) How to handle template conflicts: skip, update, or error",
    ),
    new_name: str = Query(
        default=None, description="Rename blueprint on import to avoid name conflicts"
    ),
    dockerfile_conflict_strategy: str = Query(
        default="skip",
        description="How to handle Dockerfile conflicts: skip (use existing), overwrite, or error",
    ),
    content_conflict_strategy: str = Query(
        default="skip",
        description="How to handle Content Library conflicts: skip, rename, or use_existing",
    ),
    build_images: bool = Query(
        default=True, description="Automatically build Docker images from included Dockerfiles"
    ),
    db: DBSession = None,
    current_user: CurrentUser = None,
):
    """
    Import a blueprint from a ZIP package (v3.0).

    Creates:
    - Extracted Dockerfiles to /data/images/
    - Built Docker images and BaseImage records
    - Imported Content Library items
    - The blueprint with proper references

    Options:
    - new_name: Rename the blueprint to avoid conflicts
    - dockerfile_conflict_strategy: skip (use existing), overwrite, error
    - content_conflict_strategy: skip, rename, use_existing
    - build_images: Auto-build Docker images from Dockerfiles
    """
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="File must be a ZIP archive")

    # Save uploaded file temporarily
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    try:
        content = await file.read()
        temp_file.write(content)
        temp_file.close()

        options = BlueprintImportOptions(
            template_conflict_strategy=template_conflict_strategy,
            new_name=new_name,
            dockerfile_conflict_strategy=dockerfile_conflict_strategy,
            content_conflict_strategy=content_conflict_strategy,
            build_images=build_images,
        )

        export_service = get_blueprint_export_service()
        result = export_service.import_blueprint(
            archive_path=temp_file.name,
            options=options,
            user=current_user,
            db=db,
        )

        if not result.success:
            raise HTTPException(
                status_code=400, detail=result.errors[0] if result.errors else "Import failed"
            )

        return result
    finally:
        # Clean up temp file
        try:
            os.unlink(temp_file.name)
        except Exception:
            pass


# ============ Async Import Endpoints ============


@router.post("/import/start")
async def start_async_import(
    file: UploadFile = File(...),
    template_conflict_strategy: str = Query(
        default="skip", description="(Deprecated) How to handle template conflicts"
    ),
    new_name: str = Query(
        default=None, description="Rename blueprint on import to avoid name conflicts"
    ),
    dockerfile_conflict_strategy: str = Query(
        default="skip", description="How to handle Dockerfile conflicts: skip, overwrite, or error"
    ),
    content_conflict_strategy: str = Query(
        default="skip",
        description="How to handle Content Library conflicts: skip, rename, or use_existing",
    ),
    build_images: bool = Query(
        default=True, description="Automatically build Docker images from included Dockerfiles"
    ),
    db: DBSession = None,
    current_user: CurrentUser = None,
):
    """
    Start an async blueprint import job with progress tracking.

    Saves the uploaded file, queues a Dramatiq task, and returns immediately
    with a job_id for polling status.
    """
    import uuid as uuid_mod
    from proving_ground.tasks.blueprint_import import import_blueprint_async, update_job_status

    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="File must be a ZIP archive")

    # Save uploaded file to a persistent temp path (worker needs to access it)
    settings = __import__("proving_ground.config", fromlist=["get_settings"]).get_settings()
    import_base = Path(settings.global_shared_dir) / "imports"
    import_base.mkdir(parents=True, exist_ok=True)

    job_id = str(uuid_mod.uuid4())
    archive_path = import_base / f"{job_id}.zip"

    content = await file.read()
    archive_path.write_bytes(content)

    # Initialize job status
    update_job_status(job_id, "pending", "Queued for import...", 0, 5)

    # Build options dict
    options_dict = {
        "template_conflict_strategy": template_conflict_strategy,
        "dockerfile_conflict_strategy": dockerfile_conflict_strategy,
        "content_conflict_strategy": content_conflict_strategy,
        "build_images": build_images,
    }
    if new_name:
        options_dict["new_name"] = new_name

    # Queue the task
    import_blueprint_async.send(
        job_id,
        str(archive_path),
        str(current_user.id),
        options_dict,
    )

    return {
        "job_id": job_id,
        "status": "pending",
        "message": "Import job queued",
    }


@router.get("/import/{job_id}/status")
def get_import_status(job_id: str, current_user: CurrentUser):
    """
    Get the status of an async import job.

    Returns current step, progress, and any errors or results.
    """
    from proving_ground.tasks.blueprint_import import get_job_status

    status = get_job_status(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Import job not found")

    return status


@router.post("/import/{job_id}/cancel")
def cancel_import(job_id: str, current_user: CurrentUser):
    """
    Cancel an in-progress import job.
    """
    from proving_ground.tasks.blueprint_import import cancel_job, get_job_status

    status = get_job_status(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Import job not found")

    if status.get("status") not in ("pending", "running"):
        raise HTTPException(
            status_code=400, detail=f"Cannot cancel job with status: {status.get('status')}"
        )

    if cancel_job(job_id):
        return {"message": "Import cancelled", "job_id": job_id}
    else:
        raise HTTPException(status_code=400, detail="Failed to cancel import")


# ============ Helper Functions ============


def _catalog_origin(blueprint_id: UUID, db: Session):
    """Resolve the catalog item a blueprint came from, or explain why it has none."""
    from proving_ground.services.catalog_service import CatalogService

    blueprint = db.query(RangeBlueprint).filter(RangeBlueprint.id == blueprint_id).first()
    if not blueprint:
        raise HTTPException(status_code=404, detail="Blueprint not found")

    try:
        origin = CatalogService(db).resolve_blueprint_origin(blueprint_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not origin:
        raise HTTPException(
            status_code=404,
            detail=(
                "This blueprint was not installed from a catalog, or its catalog "
                "source is no longer available, so there is nothing to contribute back to."
            ),
        )
    return blueprint, origin


def _origin_response(origin) -> BlueprintCatalogOrigin:
    return BlueprintCatalogOrigin(
        source_id=origin.source_id,
        source_name=origin.source_name,
        source_url=origin.source_url,
        source_branch=origin.source_branch,
        item_id=origin.item_id,
        item_name=origin.item_name,
        installed_version=origin.installed_version,
        item_path=origin.item_path,
    )


@router.get("/{blueprint_id}/catalog-diff", response_model=BlueprintCatalogDiff)
def get_blueprint_catalog_diff(blueprint_id: UUID, db: DBSession, current_user: CurrentUser):
    """Show which fields differ from the catalog version this blueprint came from."""
    from proving_ground.catalog.contribution import diff_blueprint_against_catalog

    blueprint, origin = _catalog_origin(blueprint_id, db)
    diff = diff_blueprint_against_catalog(
        origin.document,
        blueprint.config or {},
        name=blueprint.name,
        description=blueprint.description,
    )
    return BlueprintCatalogDiff(
        origin=_origin_response(origin),
        changes=[
            BlueprintFieldChange(
                key=c.key,
                path=list(c.path),
                kind=c.kind,
                label=c.label,
                before=c.before,
                after=c.after,
            )
            for c in diff.changes
        ],
        notes=diff.notes,
        has_changes=bool(diff),
    )


@router.post("/{blueprint_id}/catalog-contribution", response_model=BlueprintContribution)
def build_blueprint_catalog_contribution(
    blueprint_id: UUID,
    data: BlueprintContributionRequest,
    db: DBSession,
    current_user: CurrentUser,
):
    """Render selected local changes as a patch against the catalog's blueprint.yaml.

    Nothing is sent anywhere: the patch and the updated file come back to the
    caller, who applies them to a catalog clone and opens the pull request.
    """
    from proving_ground.catalog.contribution import (
        build_patch,
        diff_blueprint_against_catalog,
    )

    blueprint, origin = _catalog_origin(blueprint_id, db)
    diff = diff_blueprint_against_catalog(
        origin.document,
        blueprint.config or {},
        name=blueprint.name,
        description=blueprint.description,
    )

    if data.changes is not None:
        known = {c.key for c in diff.changes}
        unknown = sorted(set(data.changes) - known)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No such change(s): {', '.join(unknown)}. The blueprint may have "
                    "changed since the diff was loaded; reload it and try again."
                ),
            )

    selected = diff.select(data.changes)
    if not selected:
        raise HTTPException(
            status_code=400,
            detail="No changes selected, so there is nothing to contribute.",
        )

    try:
        result = build_patch(
            origin.document,
            selected,
            item_path=origin.item_path,
            original_text=origin.text,
            notes=diff.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return BlueprintContribution(
        origin=_origin_response(origin),
        patch=result.patch,
        blueprint_yaml=result.blueprint_yaml,
        applied=[c.key for c in result.applied],
        notes=result.notes,
        applies_to_source=result.applies_to_source,
        suggested_filename=f"{origin.item_id}-contribution.patch",
    )


def _blueprint_to_response(blueprint: RangeBlueprint, db: Session) -> BlueprintResponse:
    network_count, machine_count = _counts(blueprint.config)
    return BlueprintResponse(
        id=blueprint.id,
        name=blueprint.name,
        description=blueprint.description,
        version=blueprint.version,
        base_subnet_prefix=blueprint.base_subnet_prefix,
        next_offset=blueprint.next_offset,
        content_ids=blueprint.content_ids or [],
        created_by=blueprint.created_by,
        created_at=blueprint.created_at,
        updated_at=blueprint.updated_at,
        network_count=network_count,
        vm_count=machine_count,
        schema_version=_schema_version(blueprint.config),
        instance_count=len(blueprint.instances),
        is_seed=blueprint.is_seed if hasattr(blueprint, "is_seed") else False,
    )


def _blueprint_to_detail_response(
    blueprint: RangeBlueprint, config: Optional[Mapping[str, Any]], username: str = None
) -> BlueprintDetailResponse:
    config = dict(config or {})
    network_count, machine_count = _counts(config)
    return BlueprintDetailResponse(
        id=blueprint.id,
        name=blueprint.name,
        description=blueprint.description,
        version=blueprint.version,
        base_subnet_prefix=blueprint.base_subnet_prefix,
        next_offset=blueprint.next_offset,
        content_ids=blueprint.content_ids or [],
        created_by=blueprint.created_by,
        created_at=blueprint.created_at,
        updated_at=blueprint.updated_at,
        network_count=network_count,
        vm_count=machine_count,
        schema_version=_schema_version(config),
        instance_count=len(blueprint.instances) if hasattr(blueprint, "instances") else 0,
        config=config,
        created_by_username=(
            username
            if username
            else (
                "PROVING GROUND" if (hasattr(blueprint, "is_seed") and blueprint.is_seed) else None
            )
        ),
        is_seed=blueprint.is_seed if hasattr(blueprint, "is_seed") else False,
    )


def _instance_to_response(instance: RangeInstance, db: Session) -> InstanceResponse:
    from proving_ground.models import User

    range_obj = instance.range
    instructor = db.query(User).filter(User.id == instance.instructor_id).first()

    return InstanceResponse(
        id=instance.id,
        name=instance.name,
        blueprint_id=instance.blueprint_id,
        blueprint_version=instance.blueprint_version,
        subnet_offset=instance.subnet_offset,
        instructor_id=instance.instructor_id,
        range_id=instance.range_id,
        created_at=instance.created_at,
        range_name=range_obj.name if range_obj else None,
        range_status=range_obj.status.value if range_obj else None,
        instructor_username=instructor.username if instructor else None,
    )
