# backend/proving_ground/api/events.py
from uuid import UUID
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from proving_ground.api.deps import get_db, get_current_user, check_range_access
from proving_ground.models.user import User
from proving_ground.models.vm import VM
from proving_ground.models.event_log import EventType
from proving_ground.schemas.event_log import EventLogResponse, EventLogList
from proving_ground.services.event_service import EventService

# The event log belongs to a range and answers to the range's read rule, via
# the shared helper rather than a fourth owner-only comparison of its own.
# The comparison this replaced granted nobody but the creator: an administrator
# was refused on a range they did not make, so the person asked to explain a
# failed deploy could not read the placement reason the worker wrote here, and
# the learner the range is assigned to was refused their own range's history.
router = APIRouter(prefix="/events", tags=["events"])


@router.get("/{range_id}", response_model=EventLogList)
def get_range_events(
    range_id: UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    event_types: Optional[List[EventType]] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    check_range_access(range_id, current_user, db)

    service = EventService(db)
    events, total = service.get_events(range_id, limit, offset, event_types)

    return EventLogList(events=[EventLogResponse.model_validate(e) for e in events], total=total)


@router.get("/vm/{vm_id}", response_model=List[EventLogResponse])
def get_vm_events(
    vm_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")

    # Also the 404 for a VM whose range row is gone. The comparison this
    # replaced dereferenced that fetch unchecked and answered with a 500.
    check_range_access(vm.range_id, current_user, db)

    service = EventService(db)
    events = service.get_vm_events(vm_id, limit)

    return [EventLogResponse.model_validate(e) for e in events]
