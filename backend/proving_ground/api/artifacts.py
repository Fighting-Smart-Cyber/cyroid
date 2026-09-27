# proving_ground/api/artifacts.py
"""API endpoints for artifact management.

The library half -- upload, list, download -- is substrate-neutral: files in object storage with
provenance attached. *Placement* is not. Placement copies a stored file into a running range
machine, and the only mechanism for that is the Docker SDK reaching a container id, which a
KubeVirt machine does not have. On the Kubernetes substrate these endpoints therefore refuse by
name rather than fail on "VM has no running container", which reads as a machine somebody forgot
to switch on rather than a capability that is absent (MSEL-055).
"""

import asyncio
from typing import List
from uuid import UUID, uuid4
import logging

from fastapi import APIRouter, HTTPException, status, UploadFile, File, Form
from fastapi.responses import StreamingResponse
import io

from proving_ground.api import kubernetes_ranges
from proving_ground.api.deps import DBSession, CurrentUser, check_range_control

from proving_ground.models.artifact import (
    Artifact,
    ArtifactPlacement,
    ArtifactType,
    MaliciousIndicator,
    PlacementStatus,
)
from proving_ground.models.vm import VM
from proving_ground.models.range import Range
from proving_ground.models.user import User

# Who may see the artifact library at all. Artifacts are the samples an exercise is built from
# -- the model carries a `malicious_indicator` column -- so this is not a visibility question
# with a sensible "untagged means public" answer. `check_resource_access` is deliberately NOT
# used here for that reason: its no-tags-is-public rule would hand every learner the whole
# library the moment a nav entry existed. Mirrors content.py's AUTHORING_ROLES.
ARTIFACT_LIBRARY_ROLES = ("admin", "engineer")


def may_browse_artifacts(user: User) -> bool:
    return bool(user.is_admin or user.has_any_role(*ARTIFACT_LIBRARY_ROLES))


def check_artifact_access(artifact: "Artifact", current_user: User) -> None:
    """May this account see this artifact? Library roles, or the account that uploaded it."""
    if may_browse_artifacts(current_user) or artifact.uploaded_by == current_user.id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="The artifact library is available to engineers and administrators.",
    )


def check_artifact_control(artifact: "Artifact", current_user: User) -> None:
    """May this account CHANGE it? Only its uploader, or an administrator.

    Stricter than access on purpose, and for the same reason check_resource_control is stricter
    than check_resource_access: one engineer editing or deleting another engineer's sample is
    not something "may I look at this" should ever have implied.
    """
    if current_user.is_admin or artifact.uploaded_by == current_user.id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only the account that uploaded this artifact, or an administrator, may change it.",
    )


from proving_ground.schemas.artifact import (
    ArtifactUpdate,
    ArtifactResponse,
    ArtifactPlacementCreate,
    ArtifactPlacementResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/artifacts", tags=["Artifacts"])

PLACEMENT_SUBSTRATE_REFUSAL = (
    "This install runs on the Kubernetes substrate, where a range's machines are KubeVirt "
    "virtual machines. Copying a file into a running machine needs a guest-side path this "
    "platform does not have yet, so nothing was placed. The artifact itself is stored and "
    "can still be downloaded."
)


def get_storage_service():
    """Lazy import storage service."""
    from proving_ground.services.storage_service import get_storage_service as _get_storage

    return _get_storage()


def get_docker_service():
    """Lazy import docker service."""
    from proving_ground.services.docker_service import get_docker_service as _get_docker

    return _get_docker()


@router.get("", response_model=List[ArtifactResponse])
def list_artifacts(db: DBSession, current_user: CurrentUser):
    """The artifacts this account may see.

    It returned every artifact in the install to anyone signed in. That was invisible only
    because nothing linked to it; a nav entry would have turned it into an enumeration of every
    sample on the platform.
    """
    query = db.query(Artifact)
    if not may_browse_artifacts(current_user):
        query = query.filter(Artifact.uploaded_by == current_user.id)
    return query.all()


@router.post("/upload", response_model=ArtifactResponse, status_code=status.HTTP_201_CREATED)
async def upload_artifact(
    db: DBSession,
    current_user: CurrentUser,
    file: UploadFile = File(...),
    name: str = Form(...),
    description: str = Form(None),
    artifact_type: str = Form("other"),
    malicious_indicator: str = Form("safe"),
    ttps: str = Form(None),
    tags: str = Form(None),
):
    """Upload an artifact. Authoring is a library role, not something every account may do."""
    if not may_browse_artifacts(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The artifact library is available to engineers and administrators.",
        )
    """Upload a new artifact."""
    storage = get_storage_service()

    # Generate unique file path
    artifact_id = uuid4()
    object_name = f"artifacts/{artifact_id}/{file.filename}"

    # Upload to MinIO
    sha256_hash, file_size = storage.upload_file(
        file.file,
        object_name,
        content_type=file.content_type or "application/octet-stream",
    )

    # Parse list fields
    ttps_list = [t.strip() for t in ttps.split(",")] if ttps else []
    tags_list = [t.strip() for t in tags.split(",")] if tags else []

    # Create database record
    artifact = Artifact(
        id=artifact_id,
        name=name,
        description=description,
        file_path=object_name,
        sha256_hash=sha256_hash,
        file_size=file_size,
        artifact_type=ArtifactType(artifact_type),
        malicious_indicator=MaliciousIndicator(malicious_indicator),
        ttps=ttps_list,
        tags=tags_list,
        uploaded_by=current_user.id,
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)

    return artifact


@router.get("/{artifact_id}", response_model=ArtifactResponse)
def get_artifact(artifact_id: UUID, db: DBSession, current_user: CurrentUser):
    """Get artifact details."""
    artifact = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    if not artifact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact not found",
        )
    check_artifact_access(artifact, current_user)
    return artifact


@router.put("/{artifact_id}", response_model=ArtifactResponse)
def update_artifact(
    artifact_id: UUID,
    artifact_data: ArtifactUpdate,
    db: DBSession,
    current_user: CurrentUser,
):
    """Update artifact metadata."""
    artifact = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    if not artifact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact not found",
        )
    check_artifact_control(artifact, current_user)

    update_data = artifact_data.model_dump(exclude_unset=True)

    # Handle enum fields
    if "artifact_type" in update_data:
        update_data["artifact_type"] = ArtifactType(update_data["artifact_type"])
    if "malicious_indicator" in update_data:
        update_data["malicious_indicator"] = MaliciousIndicator(update_data["malicious_indicator"])

    for field, value in update_data.items():
        setattr(artifact, field, value)

    db.commit()
    db.refresh(artifact)
    return artifact


@router.delete("/{artifact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_artifact(artifact_id: UUID, db: DBSession, current_user: CurrentUser):
    """Delete an artifact."""
    artifact = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    if not artifact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact not found",
        )
    check_artifact_control(artifact, current_user)

    # Delete from storage
    storage = get_storage_service()
    storage.delete_file(artifact.file_path)

    # Delete placements
    db.query(ArtifactPlacement).filter(ArtifactPlacement.artifact_id == artifact_id).delete()

    db.delete(artifact)
    db.commit()


@router.get("/{artifact_id}/download")
def download_artifact(artifact_id: UUID, db: DBSession, current_user: CurrentUser):
    """Download an artifact file."""
    # The bytes, not just the metadata -- this is the endpoint that actually hands over a sample.
    artifact = db.query(Artifact).filter(Artifact.id == artifact_id).first()
    if not artifact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact not found",
        )
    check_artifact_access(artifact, current_user)

    storage = get_storage_service()
    file_data = storage.download_file(artifact.file_path)
    if not file_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found in storage",
        )

    filename = artifact.file_path.split("/")[-1]
    return StreamingResponse(
        io.BytesIO(file_data),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# Artifact Placement endpoints


@router.post(
    "/placements", response_model=ArtifactPlacementResponse, status_code=status.HTTP_201_CREATED
)
def create_placement(
    placement_data: ArtifactPlacementCreate,
    db: DBSession,
    current_user: CurrentUser,
):
    """Create an artifact placement."""
    if kubernetes_ranges.is_kubernetes():
        # Refused at creation, not only at execution. A Kubernetes range has no VM rows, so this
        # would answer "VM not found" for every machine the range actually has -- and a placement
        # that did somehow get created could never be executed, leaving a staged action on an
        # exercise plan that will silently never happen.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=PLACEMENT_SUBSTRATE_REFUSAL,
        )

    # Verify artifact exists
    artifact = db.query(Artifact).filter(Artifact.id == placement_data.artifact_id).first()
    if not artifact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact not found",
        )

    # Verify VM exists
    vm = db.query(VM).filter(VM.id == placement_data.vm_id).first()
    if not vm:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="VM not found",
        )

    # Staging a file for someone's machine is a change to their range, and a range's VMs have no
    # owner of their own -- so the range's rule is the one that applies.
    check_range_control(vm.range_id, current_user, db)

    placement = ArtifactPlacement(**placement_data.model_dump())
    db.add(placement)
    db.commit()
    db.refresh(placement)
    return placement


@router.get("/placements", response_model=List[ArtifactPlacementResponse])
def list_placements(
    vm_id: UUID = None,
    artifact_id: UUID = None,
    db: DBSession = None,
    current_user: CurrentUser = None,
):
    """List artifact placements.

    Returned every placement on the install to anyone signed in, which is a map of which sample
    sits on which machine in whose exercise. Scoped to the library roles, like the library.
    """
    if not may_browse_artifacts(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The artifact library is available to engineers and administrators.",
        )
    query = db.query(ArtifactPlacement)
    if vm_id:
        query = query.filter(ArtifactPlacement.vm_id == vm_id)
    if artifact_id:
        query = query.filter(ArtifactPlacement.artifact_id == artifact_id)
    return query.all()


@router.post("/placements/{placement_id}/execute", response_model=ArtifactPlacementResponse)
def execute_placement(
    placement_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Execute an artifact placement (copy file to VM)."""
    placement = db.query(ArtifactPlacement).filter(ArtifactPlacement.id == placement_id).first()
    if not placement:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Placement not found",
        )

    if kubernetes_ranges.is_kubernetes():
        # Left PENDING rather than marked FAILED: nothing was attempted, and a placement staged
        # on a host that later grows a guest-side path should still be runnable then.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=PLACEMENT_SUBSTRATE_REFUSAL,
        )

    if placement.status != PlacementStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot execute placement in {placement.status} status",
        )

    artifact = db.query(Artifact).filter(Artifact.id == placement.artifact_id).first()
    vm = db.query(VM).filter(VM.id == placement.vm_id).first()

    if not artifact or not vm:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact or VM not found",
        )

    # Writing a file into somebody's machine is a change to their range.
    check_range_control(vm.range_id, current_user, db)

    if not vm.container_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="VM has no running container",
        )

    placement.status = PlacementStatus.IN_PROGRESS
    db.commit()

    try:
        # Download from storage
        storage = get_storage_service()
        file_data = storage.download_file(artifact.file_path)
        if not file_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found in storage",
            )

        # Save to temp file and copy to container
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(file_data)
            tmp_path = tmp.name

        try:
            docker = get_docker_service()
            target_dir = os.path.dirname(placement.target_path)

            # Check for DinD mode
            range_obj = db.query(Range).filter(Range.id == vm.range_id).first()
            use_dind = bool(range_obj and range_obj.dind_docker_url)

            if use_dind:
                asyncio.run(
                    docker.copy_to_container_dind(
                        range_id=str(vm.range_id),
                        docker_url=range_obj.dind_docker_url,
                        container_id=vm.container_id,
                        src_path=tmp_path,
                        dst_path=target_dir,
                    )
                )
            else:
                docker.copy_to_container(vm.container_id, tmp_path, target_dir)

            from datetime import datetime, timezone

            placement.status = PlacementStatus.PLACED
            # Timezone-aware: the column is DateTime(timezone=True), and a naive UTC value
            # stored in it reads back as local time when an exercise is reconstructed.
            placement.placement_time = datetime.now(timezone.utc)
            placement.error_message = None
            db.commit()
            db.refresh(placement)

        finally:
            os.unlink(tmp_path)

    except HTTPException as exc:
        # The storage miss above is a 404 about the artifact, not a placement crash. Letting the
        # blanket handler below catch it turned it into a 500 that named neither.
        placement.status = PlacementStatus.FAILED
        placement.error_message = str(exc.detail)
        db.commit()
        raise
    except Exception as e:
        logger.error(f"Failed to execute placement {placement_id}: {e}")
        placement.status = PlacementStatus.FAILED
        # Without this the record showed FAILED and nothing else, so nobody could tell a wrong
        # target path from a machine that had gone away.
        placement.error_message = str(e)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to place artifact: {str(e)}",
        ) from e

    return placement


@router.delete("/placements/{placement_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_placement(
    placement_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Delete an artifact placement."""
    placement = db.query(ArtifactPlacement).filter(ArtifactPlacement.id == placement_id).first()
    if not placement:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Placement not found",
        )

    # Removing a staged action from somebody's exercise is a change to their range. A placement
    # whose machine has already been deleted has nothing left to authorise against, and deleting
    # the orphan is how it gets cleaned up.
    vm = db.query(VM).filter(VM.id == placement.vm_id).first()
    if vm:
        check_range_control(vm.range_id, current_user, db)

    db.delete(placement)
    db.commit()
