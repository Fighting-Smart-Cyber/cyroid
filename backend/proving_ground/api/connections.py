# backend/proving_ground/api/connections.py
from uuid import UUID
from typing import Optional, List
from pydantic import BaseModel
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from proving_ground.api.deps import get_db, get_current_user, check_range_access
from proving_ground.models.user import User
from proving_ground.models.vm import VM
from proving_ground.models.connection import ConnectionProtocol, ConnectionState
from proving_ground.services.connection_service import ConnectionService

# Observed traffic is a read of the range, so it answers to the range's read
# rule through the shared helper. The owner-only comparison this replaced
# refused an administrator on a range they did not create, and refused the
# learner it is assigned to, on their own range's traffic.
router = APIRouter(prefix="/connections", tags=["connections"])


class ConnectionResponse(BaseModel):
    id: UUID
    range_id: UUID
    src_vm_id: Optional[UUID]
    src_ip: str
    src_port: int
    dst_vm_id: Optional[UUID]
    dst_ip: str
    dst_port: int
    protocol: ConnectionProtocol
    state: ConnectionState
    bytes_sent: int
    bytes_received: int
    started_at: datetime
    ended_at: Optional[datetime]

    class Config:
        from_attributes = True


class ConnectionListResponse(BaseModel):
    connections: List[ConnectionResponse]
    total: int


@router.get("/{range_id}", response_model=ConnectionListResponse)
def get_range_connections(
    range_id: UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    active_only: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get connections for a range."""
    check_range_access(range_id, current_user, db)

    service = ConnectionService(db)
    connections, total = service.get_connections(range_id, limit, offset, active_only)

    return ConnectionListResponse(
        connections=[ConnectionResponse.model_validate(c) for c in connections], total=total
    )


@router.get("/vm/{vm_id}", response_model=List[ConnectionResponse])
def get_vm_connections(
    vm_id: UUID,
    direction: str = Query("both", pattern="^(both|incoming|outgoing)$"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get connections for a specific VM."""
    vm = db.query(VM).filter(VM.id == vm_id).first()
    if not vm:
        raise HTTPException(status_code=404, detail="VM not found")

    # Also the 404 for a VM whose range row is gone. The comparison this
    # replaced dereferenced that fetch unchecked and answered with a 500.
    check_range_access(vm.range_id, current_user, db)

    service = ConnectionService(db)
    connections = service.get_vm_connections(vm_id, direction, limit)

    return [ConnectionResponse.model_validate(c) for c in connections]
