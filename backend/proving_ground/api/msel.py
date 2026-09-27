# backend/proving_ground/api/msel.py
from uuid import UUID
from typing import List, Optional, Any
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from proving_ground.api.deps import (
    get_db,
    get_current_user,
    check_range_access,
    check_range_control,
    check_resource_control,
    is_student_only,
)
from proving_ground.api.walkthrough import strip_quiz_answers
from proving_ground.models.user import User
from proving_ground.models.msel import MSEL
from proving_ground.models.range import Range
from proving_ground.models.inject import Inject, InjectStatus
from proving_ground.services.msel_parser import MSELParser
from proving_ground.services.inject_service import InjectService
from proving_ground.services.docker_service import DockerService, get_docker_service
from proving_ground.models.vm import VM

# An MSEL belongs to a range, so it answers to the range's rules rather than to
# an owner-only comparison repeated at each route -- which refused an
# administrator on a range they did not create, and which the next route added
# here would have been written without.
#
# Reading the scenario is the range's read rule. Importing one, deleting one,
# and firing or skipping an inject all change the exercise -- firing one runs
# commands inside its machines -- so those take control, which is owner or
# admin and consults no tag. Widening them to the read rule would let anyone
# who may watch an exercise drive it.
router = APIRouter(prefix="/msel", tags=["msel"])


def _is_the_audience(range_obj: Range, current_user: User, db: Session) -> bool:
    """True when this caller is the exercise's audience rather than its staff.

    Ownership is asked before the role is, because anyone may create a range:
    a student-only account that imported this MSEL is its own white cell and
    must not have its own document withheld from it.

    Control is the shared check and it raises instead of returning False, so
    that a caller standing at a gate cannot ignore the answer. Here the answer
    itself is what is wanted, not the refusal, so the refusal is caught.
    """
    if not is_student_only(current_user):
        return False
    try:
        check_resource_control("range", range_obj.id, current_user, db, range_obj.created_by)
    except HTTPException:
        return True
    return False


class MSELImport(BaseModel):
    name: str
    content: str


class InjectResponse(BaseModel):
    id: UUID
    sequence_number: int
    inject_time_minutes: int
    title: str
    description: Optional[str]
    actions: List[Any]
    status: str
    executed_at: Optional[datetime]

    class Config:
        from_attributes = True


class MSELResponse(BaseModel):
    id: UUID
    name: str
    range_id: UUID
    content: Optional[str] = None
    walkthrough: Optional[dict] = None
    injects: List[InjectResponse]

    class Config:
        from_attributes = True


@router.post("/{range_id}/import", status_code=201, response_model=MSELResponse)
def import_msel(
    range_id: UUID,
    data: MSELImport,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Import an MSEL document for a range."""
    check_range_control(range_id, current_user, db)

    # Delete existing MSEL if any
    existing = db.query(MSEL).filter(MSEL.range_id == range_id).first()
    if existing:
        db.delete(existing)
        db.commit()

    # Parse MSEL
    parser = MSELParser()
    parsed_injects = parser.parse(data.content)
    walkthrough = parser.parse_walkthrough(data.content)

    # Create MSEL
    msel = MSEL(range_id=range_id, name=data.name, content=data.content, walkthrough=walkthrough)
    db.add(msel)
    db.commit()
    db.refresh(msel)

    # Create Injects
    for inject_data in parsed_injects:
        inject = Inject(
            msel_id=msel.id,
            sequence_number=inject_data["sequence_number"],
            inject_time_minutes=inject_data["inject_time_minutes"],
            title=inject_data["title"],
            description=inject_data.get("description", ""),
            actions=inject_data["actions"],
        )
        db.add(inject)

    db.commit()

    # Return with injects
    injects = (
        db.query(Inject).filter(Inject.msel_id == msel.id).order_by(Inject.sequence_number).all()
    )

    return MSELResponse(
        id=msel.id,
        name=msel.name,
        range_id=msel.range_id,
        walkthrough=msel.walkthrough,
        injects=[
            InjectResponse(
                id=i.id,
                sequence_number=i.sequence_number,
                inject_time_minutes=i.inject_time_minutes,
                title=i.title,
                description=i.description,
                actions=i.actions or [],
                status=i.status.value,
                executed_at=i.executed_at,
            )
            for i in injects
        ],
    )


@router.get("/{range_id}", response_model=MSELResponse)
def get_msel(
    range_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    """Get the MSEL for a range."""
    range_obj = check_range_access(range_id, current_user, db)

    msel = db.query(MSEL).filter(MSEL.range_id == range_id).first()
    if not msel:
        raise HTTPException(status_code=404, detail="No MSEL found for this range")

    # The learner's lab page reads this route, because an instructor may write
    # the guide inside the MSEL rather than linking one from the library, and
    # that guide is the only part of an MSEL written for the learner. The rest
    # is written about them: the raw scenario, and an inject timeline saying
    # what will be done to their machines and when. api/content.py says the
    # same thing about the MSEL content type -- never handed to a learner, not
    # even once published -- and this route is the other way to the same
    # document.
    #
    # The guide goes through the same answer-key strip as api/walkthrough.py,
    # for the same reason: every option arrives carrying its own `correct`
    # flag, and a Knowledge Check whose key was in the page source is not
    # evidence of anything.
    if _is_the_audience(range_obj, current_user, db):
        return MSELResponse(
            id=msel.id,
            name=msel.name,
            range_id=msel.range_id,
            content=None,
            walkthrough=strip_quiz_answers(msel.walkthrough, None),
            injects=[],
        )

    injects = (
        db.query(Inject).filter(Inject.msel_id == msel.id).order_by(Inject.sequence_number).all()
    )

    return MSELResponse(
        id=msel.id,
        name=msel.name,
        range_id=msel.range_id,
        content=msel.content,
        walkthrough=msel.walkthrough,
        injects=[
            InjectResponse(
                id=i.id,
                sequence_number=i.sequence_number,
                inject_time_minutes=i.inject_time_minutes,
                title=i.title,
                description=i.description,
                actions=i.actions or [],
                status=i.status.value,
                executed_at=i.executed_at,
            )
            for i in injects
        ],
    )


@router.delete("/{range_id}", status_code=204)
def delete_msel(
    range_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    """Delete the MSEL for a range."""
    check_range_control(range_id, current_user, db)

    msel = db.query(MSEL).filter(MSEL.range_id == range_id).first()
    if not msel:
        raise HTTPException(status_code=404, detail="No MSEL found for this range")

    db.delete(msel)
    db.commit()


class InjectExecutionResponse(BaseModel):
    success: bool
    inject_id: UUID
    status: str
    results: List[Any]


@router.post("/inject/{inject_id}/execute", response_model=InjectExecutionResponse)
def execute_inject(
    inject_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    docker_service: DockerService = Depends(get_docker_service),
):
    """Execute an inject manually."""
    inject = db.query(Inject).filter(Inject.id == inject_id).first()
    if not inject:
        raise HTTPException(status_code=404, detail="Inject not found")

    msel = db.query(MSEL).filter(MSEL.id == inject.msel_id).first()
    if not msel:
        raise HTTPException(status_code=404, detail="MSEL not found")

    range_obj = check_range_control(msel.range_id, current_user, db)

    # Check inject status
    if inject.status == InjectStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Inject already executed")
    if inject.status == InjectStatus.EXECUTING:
        raise HTTPException(status_code=400, detail="Inject already executing")

    # Build VM map by hostname
    vms = db.query(VM).filter(VM.range_id == range_obj.id).all()
    vm_map = {vm.hostname: vm for vm in vms}

    # Execute inject
    service = InjectService(db, docker_service)
    result = service.execute_inject(inject, vm_map)

    return InjectExecutionResponse(
        success=result["success"],
        inject_id=inject.id,
        status=inject.status.value,
        results=result.get("results", []),
    )


@router.post("/inject/{inject_id}/skip", status_code=200)
def skip_inject(
    inject_id: UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    """Skip an inject."""
    inject = db.query(Inject).filter(Inject.id == inject_id).first()
    if not inject:
        raise HTTPException(status_code=404, detail="Inject not found")

    msel = db.query(MSEL).filter(MSEL.id == inject.msel_id).first()
    if not msel:
        # An inject whose MSEL row is gone was a 500 here: the fetch was
        # dereferenced unchecked on the very next line.
        raise HTTPException(status_code=404, detail="MSEL not found")

    check_range_control(msel.range_id, current_user, db)

    if inject.status != InjectStatus.PENDING:
        raise HTTPException(status_code=400, detail="Can only skip pending injects")

    inject.status = InjectStatus.SKIPPED
    inject.execution_log = "Skipped by user"
    db.commit()

    return {"status": "skipped", "inject_id": str(inject.id)}
