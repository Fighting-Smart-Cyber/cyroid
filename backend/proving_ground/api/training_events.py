# backend/proving_ground/api/training_events.py
"""Training Events API endpoints for scheduling and role-based content delivery."""

import logging
from datetime import datetime
from typing import Annotated, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from proving_ground.api.deps import get_current_user, get_db
from proving_ground.capability import Delivery
from proving_ground.models.user import User
from proving_ground.models.event import TrainingEvent, EventParticipant, EventStatus
from proving_ground.models.content import Content
from proving_ground.models.blueprint import RangeBlueprint
from proving_ground.schemas.event import (
    EventCreate,
    EventUpdate,
    EventResponse,
    EventListResponse,
    EventDetailResponse,
    EventParticipantCreate,
    EventParticipantResponse,
    EventBriefingResponse,
    EventContentItem,
    ParticipantRole,
    VMVisibilityUpdate,
    VMVisibilityResponse,
    VMVisibilityVM,
    BulkVMVisibilityUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/training-events", tags=["training-events"])

# Type aliases
DBSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def can_manage_event(event: TrainingEvent, user: User) -> bool:
    """Check if user can manage (edit/delete) an event."""
    return event.created_by_id == user.id or user.is_admin


def can_view_event(event: TrainingEvent, user: User) -> bool:
    """Check if user can view an event based on roles/tags."""
    # Admin can see all
    if user.is_admin:
        return True
    # Owner can see their events
    if event.created_by_id == user.id:
        return True
    # Check role-based access
    if event.allowed_roles:
        user_roles = user.roles or []
        if any(role in event.allowed_roles for role in user_roles):
            return True
    # Check tag-based access
    if event.tags:
        user_tags = user.tags or []
        if any(tag in event.tags for tag in user_tags):
            return True
    # If no roles/tags specified, event is public
    if not event.allowed_roles and not event.tags:
        return True
    return False


def get_user_event_role(event: TrainingEvent, user: User, db: Session) -> str:
    """The user's role in one event, which is what the briefing filters on.

    This is the one place the two role vocabularies meet, and the direction is deliberate: a
    registered participant's event role wins, and only somebody who is *not* a participant falls
    back to what their platform role implies they would be.
    """
    # Check if they're registered as a participant
    participant = (
        db.query(EventParticipant)
        .filter(
            EventParticipant.event_id == event.id,
            EventParticipant.user_id == user.id,
        )
        .first()
    )
    if participant:
        return participant.role
    # If they're the creator, they're the instructor
    if event.created_by_id == user.id:
        return ParticipantRole.INSTRUCTOR.value
    # Default based on their global role
    if user.has_role("admin") or user.has_role("engineer"):
        return ParticipantRole.INSTRUCTOR.value
    if user.has_role("evaluator"):
        return ParticipantRole.EVALUATOR.value
    return ParticipantRole.STUDENT.value


def cohort_range(event_id: UUID, db: Session):
    """The one range a team exercise shares, or None if this event is self-paced.

    A team exercise is an event that owns exactly one range and has assigned nobody to it: that
    is the shape `placement_for_assignment` reads to resolve the range to a vcluster rather than
    to a per-learner namespace. Asking the same question here, rather than storing the mode on
    the event, is what stops the API's account of an event and the substrate's treatment of its
    range from drifting apart.

    The count is load-bearing rather than decoration. `Range.assigned_to_user_id` is
    ondelete="SET NULL", so a self-paced range outlives the learner it belonged to and comes back
    as a range under an event with nobody assigned -- on the unassigned test alone, identical to
    a shared lab. A twelve-student self-paced class whose one departed student's account was
    deleted then described itself as a team exercise, and the next student added to it was linked
    to the departed learner's range, with console access to it, instead of being given one of
    their own. A team exercise only ever has the single range, so the count is what tells the two
    apart. LIMIT 2 because one extra row is all it takes to answer "more than one".
    """
    from proving_ground.models.range import Range

    owned = db.query(Range).filter(Range.training_event_id == event_id).limit(2).all()
    if len(owned) == 1 and owned[0].assigned_to_user_id is None:
        return owned[0]
    return None


def build_event_response(event: TrainingEvent, db: Session) -> dict:
    """Build event response with computed fields."""
    participant_count = (
        db.query(EventParticipant).filter(EventParticipant.event_id == event.id).count()
    )

    student_count = (
        db.query(EventParticipant)
        .filter(
            EventParticipant.event_id == event.id,
            EventParticipant.role == ParticipantRole.STUDENT.value,
        )
        .count()
    )

    blueprint_name = None
    if event.blueprint_id:
        blueprint = db.query(RangeBlueprint).filter(RangeBlueprint.id == event.blueprint_id).first()
        if blueprint:
            blueprint_name = blueprint.name

    created_by = db.query(User).filter(User.id == event.created_by_id).first()
    created_by_username = created_by.username if created_by else None

    shared = cohort_range(event.id, db)

    return {
        **{c.name: getattr(event, c.name) for c in event.__table__.columns},
        "participant_count": participant_count,
        "student_count": student_count,
        "blueprint_name": blueprint_name,
        "created_by_username": created_by_username,
        "delivery": Delivery.TEAM_EXERCISE if shared else Delivery.SELF_PACED,
        "team_range_id": shared.id if shared else None,
        "team_range_status": shared.status.value if shared else None,
        "team_range_name": shared.name if shared else None,
    }


# ============ Event CRUD ============


@router.post("", response_model=EventResponse, status_code=status.HTTP_201_CREATED)
def create_event(
    data: EventCreate,
    db: DBSession,
    current_user: CurrentUser,
):
    """Create a new training event."""
    # Validate blueprint if provided
    if data.blueprint_id:
        blueprint = db.query(RangeBlueprint).filter(RangeBlueprint.id == data.blueprint_id).first()
        if not blueprint:
            raise HTTPException(status_code=404, detail="Blueprint not found")

    # Validate content IDs if provided
    for content_id in data.content_ids:
        try:
            uuid_content_id = UUID(content_id)
            content = db.query(Content).filter(Content.id == uuid_content_id).first()
            if not content:
                raise HTTPException(status_code=404, detail=f"Content {content_id} not found")
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid content ID: {content_id}"
            ) from exc

    event = TrainingEvent(
        name=data.name,
        description=data.description,
        start_datetime=data.start_datetime,
        end_datetime=data.end_datetime,
        is_all_day=data.is_all_day,
        timezone=data.timezone,
        organization=data.organization,
        location=data.location,
        blueprint_id=data.blueprint_id,
        content_ids=data.content_ids,
        allowed_roles=data.allowed_roles,
        tags=data.tags,
        created_by_id=current_user.id,
        status=EventStatus.DRAFT,
    )

    db.add(event)
    db.commit()
    db.refresh(event)

    logger.info(f"Event created: {event.name} by {current_user.username}")
    return build_event_response(event, db)


@router.get("", response_model=List[EventListResponse])
def list_events(
    db: DBSession,
    current_user: CurrentUser,
    status_filter: Optional[EventStatus] = Query(None, alias="status"),
    start_after: Optional[datetime] = Query(None, description="Events starting after this date"),
    start_before: Optional[datetime] = Query(None, description="Events starting before this date"),
    my_events: bool = Query(False, description="Only show events I created or participate in"),
    tag: Optional[str] = Query(None, description="Filter by tag"),
    search: Optional[str] = Query(None, description="Search in name and description"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List training events with optional filters."""
    query = db.query(TrainingEvent)

    # Status filter
    if status_filter:
        query = query.filter(TrainingEvent.status == status_filter)

    # Date range filters
    if start_after:
        query = query.filter(TrainingEvent.start_datetime >= start_after)
    if start_before:
        query = query.filter(TrainingEvent.start_datetime <= start_before)

    # Tag filter
    if tag:
        query = query.filter(TrainingEvent.tags.contains([tag]))

    # Search
    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            or_(
                TrainingEvent.name.ilike(search_pattern),
                TrainingEvent.description.ilike(search_pattern),
            )
        )

    # My events filter
    if my_events:
        # Events created by user OR user is a participant
        participant_event_ids = (
            db.query(EventParticipant.event_id)
            .filter(EventParticipant.user_id == current_user.id)
            .subquery()
        )
        query = query.filter(
            or_(
                TrainingEvent.created_by_id == current_user.id,
                TrainingEvent.id.in_(participant_event_ids),
            )
        )

    # Get all matching events, then filter by visibility
    events = query.order_by(TrainingEvent.start_datetime.asc()).all()

    # Filter by visibility (role/tag access)
    visible_events = [e for e in events if can_view_event(e, current_user)]

    # Paginate
    paginated = visible_events[offset : offset + limit]

    # Build responses
    results = []
    for event in paginated:
        participant_count = (
            db.query(EventParticipant).filter(EventParticipant.event_id == event.id).count()
        )
        student_count = (
            db.query(EventParticipant)
            .filter(
                EventParticipant.event_id == event.id,
                EventParticipant.role == ParticipantRole.STUDENT.value,
            )
            .count()
        )
        results.append(
            EventListResponse(
                id=event.id,
                name=event.name,
                description=event.description,
                start_datetime=event.start_datetime,
                end_datetime=event.end_datetime,
                is_all_day=event.is_all_day,
                timezone=event.timezone,
                organization=event.organization,
                location=event.location,
                status=event.status,
                tags=event.tags,
                allowed_roles=event.allowed_roles,
                participant_count=participant_count,
                student_count=student_count,
                has_blueprint=event.blueprint_id is not None,
                created_by_id=event.created_by_id,
                created_at=event.created_at,
            )
        )

    return results


@router.get("/my-events", response_model=List[EventListResponse])
def get_my_events(
    db: DBSession,
    current_user: CurrentUser,
    status_filter: Optional[EventStatus] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Get events where the current user is a participant.

    This is a convenience endpoint for students to see their assigned events.
    Returns events where the user is registered as a participant.
    """
    # Get event IDs where user is a participant
    participant_event_ids = (
        db.query(EventParticipant.event_id)
        .filter(EventParticipant.user_id == current_user.id)
        .subquery()
    )

    query = db.query(TrainingEvent).filter(TrainingEvent.id.in_(participant_event_ids))

    # Status filter
    if status_filter:
        query = query.filter(TrainingEvent.status == status_filter)

    events = query.order_by(TrainingEvent.start_datetime.asc()).offset(offset).limit(limit).all()

    results = []
    for event in events:
        participant_count = (
            db.query(EventParticipant).filter(EventParticipant.event_id == event.id).count()
        )
        student_count = (
            db.query(EventParticipant)
            .filter(
                EventParticipant.event_id == event.id,
                EventParticipant.role == ParticipantRole.STUDENT.value,
            )
            .count()
        )

        # Get the current user's range_id for this event
        participant = (
            db.query(EventParticipant)
            .filter(
                EventParticipant.event_id == event.id, EventParticipant.user_id == current_user.id
            )
            .first()
        )
        my_range_id = participant.range_id if participant else None

        results.append(
            EventListResponse(
                id=event.id,
                name=event.name,
                description=event.description,
                start_datetime=event.start_datetime,
                end_datetime=event.end_datetime,
                is_all_day=event.is_all_day,
                timezone=event.timezone,
                organization=event.organization,
                location=event.location,
                status=event.status,
                tags=event.tags,
                allowed_roles=event.allowed_roles,
                participant_count=participant_count,
                student_count=student_count,
                has_blueprint=event.blueprint_id is not None,
                created_by_id=event.created_by_id,
                created_at=event.created_at,
                my_range_id=my_range_id,
            )
        )

    return results


@router.get("/{event_id}", response_model=EventDetailResponse)
def get_event(
    event_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Get event details by ID."""
    from proving_ground.models.range import Range

    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not can_view_event(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized to view this event")

    # Get participants with range info
    participants = db.query(EventParticipant).filter(EventParticipant.event_id == event_id).all()

    participant_responses = []
    for p in participants:
        user = db.query(User).filter(User.id == p.user_id).first()

        # Get range info if assigned
        range_status = None
        range_name = None
        if p.range_id:
            range_obj = db.query(Range).filter(Range.id == p.range_id).first()
            if range_obj:
                range_status = range_obj.status.value
                range_name = range_obj.name

        participant_responses.append(
            EventParticipantResponse(
                id=p.id,
                event_id=p.event_id,
                user_id=p.user_id,
                role=p.role,
                is_confirmed=p.is_confirmed,
                created_at=p.created_at,
                username=user.username if user else None,
                range_id=p.range_id,
                range_status=range_status,
                range_name=range_name,
            )
        )

    response_dict = build_event_response(event, db)
    response_dict["participants"] = participant_responses
    return response_dict


@router.put("/{event_id}", response_model=EventResponse)
def update_event(
    event_id: UUID,
    data: EventUpdate,
    db: DBSession,
    current_user: CurrentUser,
):
    """Update an event."""
    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not can_manage_event(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized to edit this event")

    # Can't update running/completed events (except status)
    if event.status in [EventStatus.RUNNING, EventStatus.COMPLETED]:
        if data.model_dump(exclude_unset=True).keys() - {"status"}:
            raise HTTPException(status_code=400, detail="Cannot modify running or completed events")

    update_data = data.model_dump(exclude_unset=True)

    # Validate blueprint if changed
    if "blueprint_id" in update_data and update_data["blueprint_id"]:
        blueprint = (
            db.query(RangeBlueprint)
            .filter(RangeBlueprint.id == update_data["blueprint_id"])
            .first()
        )
        if not blueprint:
            raise HTTPException(status_code=404, detail="Blueprint not found")

    for field, value in update_data.items():
        setattr(event, field, value)

    db.commit()
    db.refresh(event)

    logger.info(f"Event updated: {event.name} by {current_user.username}")
    return build_event_response(event, db)


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_event(
    event_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Delete an event and all associated participant ranges.

    Works for any event status - running events will have their ranges
    torn down automatically before deletion.
    """
    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not can_manage_event(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized to delete this event")

    # Delete all participant ranges first (handles running VMs)
    deleted_count = _delete_event_ranges(
        event_id,
        db,
        outcome=(
            "The event was kept, because its participants are the only remaining link to "
            "them. Delete it again once the cluster is reachable."
        ),
    )
    if deleted_count > 0:
        logger.info(f"Deleted {deleted_count} ranges for event {event.name}")

    db.delete(event)
    db.commit()

    logger.info(
        f"Event deleted: {event.name} (status was: {event.status.value}) by {current_user.username}"
    )


# ============ Event Status Management ============


@router.post("/{event_id}/publish", response_model=EventResponse)
def publish_event(
    event_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Publish (schedule) an event."""
    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not can_manage_event(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized")

    if event.status != EventStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Only draft events can be published")

    event.status = EventStatus.SCHEDULED
    db.commit()
    db.refresh(event)

    logger.info(f"Event published: {event.name}")
    return build_event_response(event, db)


def _readable_blueprint(db: Session, event: TrainingEvent):
    """The event's blueprint, parsed, plus the Era A config the Docker deploy needs from it.

    Returns `(blueprint, blueprint_config)` where `blueprint_config` is None for an Era B
    blueprint: it keeps its networks and workloads in the config and they are realised on the
    cluster at deploy time, so it has no Era A rows to build. Validating one against
    `BlueprintConfig` -- the Era A shape of networks and VMs -- is what refused every v2
    blueprint here outright.
    """
    from proving_ground.api import kubernetes_ranges
    from proving_ground.capability.blueprint import read_blueprint
    from proving_ground.schemas.blueprint import BlueprintConfig

    blueprint = db.query(RangeBlueprint).filter(RangeBlueprint.id == event.blueprint_id).first()
    if not blueprint:
        raise HTTPException(status_code=404, detail="Blueprint not found")

    try:
        spec = read_blueprint(blueprint.config)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid blueprint configuration: {e}") from e

    if not spec.is_legacy and not kubernetes_ranges.is_kubernetes():
        # The Docker deploy builds a range out of Network and VM rows and a v2 blueprint has
        # none to give it, so every learner would get an empty range under an event that
        # reports itself running. The Era A path used to refuse this only as a side effect
        # of BlueprintConfig rejecting the config, which the branch below no longer reaches.
        raise HTTPException(
            status_code=400,
            detail=(
                f"Blueprint '{blueprint.name}' is a v{spec.schema_version} (Era B) "
                "blueprint, which the Docker substrate cannot deploy: its workloads are "
                "realised on a cluster, and the ranges started here would be empty. Start "
                "this event from an Era A blueprint, or on a Kubernetes install."
            ),
        )

    if not spec.is_legacy:
        return blueprint, None

    try:
        blueprint_config = BlueprintConfig.model_validate(blueprint.config)
        # Include linked content from blueprint model (saved separately from config JSON)
        if blueprint.content_ids:
            blueprint_config.content_ids = blueprint.content_ids
    except Exception as e:
        logger.error(f"Failed to parse blueprint config: {e}")
        raise HTTPException(
            status_code=400, detail=f"Invalid blueprint configuration: {str(e)}"
        ) from e
    return blueprint, blueprint_config


def _range_from_blueprint(
    db: Session,
    *,
    blueprint: RangeBlueprint,
    blueprint_config,
    name: str,
    created_by: UUID,
):
    """Create one range from a blueprint, whichever era wrote it."""
    from proving_ground.models.blueprint import RangeInstance
    from proving_ground.models.range import Range, RangeStatus
    from proving_ground.services.blueprint_service import (
        create_range_from_blueprint,
        next_instance_ordinal,
    )

    if blueprint_config is not None:
        range_obj = create_range_from_blueprint(
            db=db,
            config=blueprint_config,
            range_name=name,
            created_by=created_by,
        )
    else:
        range_obj = Range(
            name=name,
            description=f"Instance of blueprint '{blueprint.name}'",
            created_by=created_by,
            status=RangeStatus.DRAFT,
        )
        db.add(range_obj)
        db.flush()
        # The instance row is what tells the deploy which blueprint to read. Without it the
        # config resolves to {}, which reads as v1, and the range lands in ERROR claiming it
        # holds an Era A blueprint.
        db.add(
            RangeInstance(
                name=name,
                blueprint_id=blueprint.id,
                blueprint_version=blueprint.version,
                subnet_offset=next_instance_ordinal(db, blueprint.id),
                instructor_id=created_by,
                range_id=range_obj.id,
            )
        )
        # An event creates several ranges in a loop, and the ordinal is counted from the table --
        # so each instance has to be visible before the next one is numbered.
        db.flush()

    return range_obj


@router.post("/{event_id}/start", response_model=EventResponse)
def start_event(
    event_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
    auto_deploy: bool = Query(False, description="Deploy the event's labs from its blueprint"),
    delivery: Delivery = Query(
        Delivery.SELF_PACED,
        description=(
            "self-paced gives each student a lab of their own; team-exercise gives the whole "
            "cohort one lab to share"
        ),
    ),
):
    """Start an event, optionally deploying its labs from the blueprint.

    The two delivery modes differ in how many ranges exist, and that difference is the whole of
    what the placement policy needs:

    **self-paced** creates one range per student participant, each assigned to that student. Every
    learner gets a namespace of their own inside the cohort's vcluster.

    **team-exercise** creates a single range for the cohort, assigned to nobody and linked to
    every student. A range under an event with no assigned learner is what
    `capability/placement.py` resolves to a vcluster of its own: the team shares one environment
    and one blast radius, so the range itself is the isolation boundary.

    The mode is a parameter of starting rather than a column on the event because this is the
    only point at which it changes anything, and what it produces -- a cohort range, or a range
    per learner -- is afterwards the event's own record of which mode it ran in.
    """
    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not can_manage_event(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized")

    if event.status not in [EventStatus.SCHEDULED, EventStatus.DRAFT]:
        raise HTTPException(status_code=400, detail="Event cannot be started from current status")

    if delivery is Delivery.TEAM_EXERCISE:
        # Refused rather than quietly downgraded to self-paced. A team exercise is the shared
        # range; with nothing deployed there is no shared anything, and the event would report
        # itself running as a team exercise with no way for the team to be in one place.
        if not auto_deploy or not event.blueprint_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "A team exercise is one lab the cohort shares, so it needs a blueprint to "
                    "build that lab from. Assign a blueprint to this event and start it with "
                    "deployment enabled, or start it as self-paced."
                ),
            )
        existing = cohort_range(event_id, db)
        if existing is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"This event already has a shared lab ('{existing.name}'). Delete it before "
                    "starting the event again, or the cohort would end up with two."
                ),
            )

    # Auto-deploy ranges for students if requested
    to_deploy: List[str] = []
    if auto_deploy and event.blueprint_id:
        from proving_ground.api import kubernetes_ranges

        blueprint, blueprint_config = _readable_blueprint(db, event)

        # Get student participants only
        students = (
            db.query(EventParticipant)
            .filter(
                EventParticipant.event_id == event_id,
                EventParticipant.role == ParticipantRole.STUDENT.value,
            )
            .all()
        )

        def stage(range_obj) -> None:
            """Validate a range before anything is committed, then queue it after the commit.

            A 400 now beats every learner's range landing in ERROR a few seconds from now.
            Nothing is committed yet, so the refusal takes the whole cohort's rows with it
            rather than leaving half an event behind.
            """
            db.flush()  # Ensure IDs are assigned
            if kubernetes_ranges.is_kubernetes():
                kubernetes_ranges.validate_for_deploy(db, range_obj.id)
            to_deploy.append(str(range_obj.id))
            logger.info(f"Created range '{range_obj.name}' (ID: {range_obj.id})")

        if delivery is Delivery.TEAM_EXERCISE:
            logger.info(f"Event {event.name}: deploying one shared range for the cohort")
            shared = _range_from_blueprint(
                db,
                blueprint=blueprint,
                blueprint_config=blueprint_config,
                name=event.name,
                created_by=current_user.id,
            )
            # Assigned to nobody on purpose: that absence is what the placement policy reads as
            # "team exercise". Linking every student to it instead is what gives each of them
            # access to it, which `get_student_accessible_range_ids` grants through the
            # participant row.
            shared.training_event_id = event.id
            for participant in students:
                participant.range_id = shared.id
            stage(shared)
            if not students:
                logger.warning(f"Event {event.name}: a shared range with no students to share it")
        elif not students:
            logger.warning(f"Event {event.name}: No student participants to deploy ranges for")
        else:
            logger.info(f"Event {event.name}: Deploying {len(students)} ranges for students")

            for participant in students:
                user = db.query(User).filter(User.id == participant.user_id).first()
                if not user:
                    continue

                range_obj = _range_from_blueprint(
                    db,
                    blueprint=blueprint,
                    blueprint_config=blueprint_config,
                    name=f"{event.name} - {user.username}",
                    created_by=current_user.id,
                )

                # Link range to student and event
                range_obj.assigned_to_user_id = participant.user_id
                range_obj.training_event_id = event.id
                participant.range_id = range_obj.id

                stage(range_obj)

    event.status = EventStatus.RUNNING
    db.commit()
    db.refresh(event)

    # Queued after the commit, not before: the worker looks the range up by id in its own
    # session, and a task sent inside this transaction races a row it cannot yet see.
    if to_deploy:
        from proving_ground.tasks.deployment import deploy_range_task

        for range_id in to_deploy:
            deploy_range_task.send(range_id)
        logger.info(f"Event {event.name}: queued {len(to_deploy)} range deployments")

    logger.info(f"Event started: {event.name} (auto_deploy={auto_deploy}, delivery={delivery})")
    return build_event_response(event, db)


def _delete_event_ranges(
    event_id: UUID,
    db: Session,
    *,
    outcome: str = "The event was left as it was.",
) -> int:
    """Delete every range this event owns, whichever delivery mode created them.

    Content is no longer deleted with ranges - it's statically defined and shared
    across range instances. Content is only deleted when its parent blueprint is deleted.

    Returns the number of ranges deleted, and raises 502 naming any range the substrate would
    not release. On Kubernetes a participant's range is a live namespace and its row is the only
    record of which one, so a range that did not come down keeps its row and its participant
    link; ending a class must not be the thing that loses a cohort's worth of machines.
    """
    import asyncio
    from proving_ground.api import kubernetes_ranges
    from proving_ground.models.range import Range
    from proving_ground.models.blueprint import RangeInstance
    from proving_ground.services.docker_service import get_docker_service
    from proving_ground.services.dind_service import get_dind_service

    participants = (
        db.query(EventParticipant)
        .filter(EventParticipant.event_id == event_id, EventParticipant.range_id.isnot(None))
        .all()
    )

    # Keyed by range, not by participant. A team exercise links one range to every student, and
    # destroying it once is the point: a second pass would find the row already dropped and
    # report the shared lab as a range the substrate refused to release.
    linked_to: dict[UUID, List[EventParticipant]] = {}
    for participant in participants:
        linked_to.setdefault(participant.range_id, []).append(participant)

    # Every range this event owns, not only the ones a participant still points at. A team
    # exercise started with no students has a shared range nobody points at, and a range whose
    # learner's account was deleted loses its participant row with them -- both are this event's
    # to take down. Leaving one behind leaks a namespace that nothing afterwards can name, since
    # the event row is the only record that it existed.
    for owned in db.query(Range).filter(Range.training_event_id == event_id).all():
        linked_to.setdefault(owned.id, [])

    deleted_count = 0
    failures: List[str] = []
    for range_id, linked in linked_to.items():
        range_obj = db.query(Range).filter(Range.id == range_id).first()

        if range_obj and kubernetes_ranges.is_kubernetes():
            reason = kubernetes_ranges.destroy_for_cleanup(db, range_obj)
            if reason is not None:
                failures.append(f"'{range_obj.name}': {reason}")
                continue
            db.query(RangeInstance).filter(RangeInstance.range_id == range_id).delete()
            db.delete(range_obj)
            deleted_count += 1
        elif range_obj:
            # Clean up Docker resources
            try:
                docker = get_docker_service()
                dind = get_dind_service()

                # DinD container cleanup
                logger.info(f"Deleting range {range_id} (event cleanup)")
                try:
                    asyncio.run(
                        dind.delete_range_container(
                            str(range_id), volume_name=range_obj.dind_volume_name
                        )
                    )
                    try:
                        from proving_ground.tasks.pool import enqueue_pool_refill

                        enqueue_pool_refill()
                    except Exception as refill_err:
                        logger.warning(f"Could not enqueue pool refill: {refill_err}")
                except Exception as dind_err:
                    logger.debug(f"DinD cleanup skipped: {dind_err}")

                # Clear Docker client cache
                try:
                    docker.dind_service.close_range_client(str(range_id))
                except Exception:
                    pass

                # Legacy cleanup
                docker.cleanup_range(str(range_id))

            except Exception as e:
                logger.warning(f"Failed to cleanup Docker for range {range_id}: {e}")

            # Delete range instances (FK constraint)
            db.query(RangeInstance).filter(RangeInstance.range_id == range_id).delete()

            # Delete the range
            db.delete(range_obj)
            deleted_count += 1

        # Clear the range reference on everyone linked to it. Only reached for a range that is
        # gone -- a teardown that failed above skips this, so the participants still point at
        # what is still running.
        for participant in linked:
            participant.range_id = None

    if failures:
        # Commit what did come down before refusing: those namespaces are gone and their rows
        # have to go with them. What is left is named, because from here only an operator can
        # decide whether to retry or to go and look at the cluster.
        db.commit()
        raise HTTPException(
            status_code=502,
            detail=f"{len(failures)} range(s) were not torn down: {'; '.join(failures)}. {outcome}",
        )

    return deleted_count


@router.post("/{event_id}/complete", response_model=EventResponse)
def complete_event(
    event_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
    cleanup_ranges: bool = Query(True, description="Delete participant ranges on completion"),
):
    """Mark an event as completed.

    If cleanup_ranges=True (default), all participant ranges will be deleted.
    """
    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not can_manage_event(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized")

    # Delete participant ranges if requested
    deleted_count = 0
    if cleanup_ranges:
        deleted_count = _delete_event_ranges(
            event_id,
            db,
            outcome=(
                "The event was not completed. Retry, or complete it with cleanup_ranges=false "
                "to close the event and leave those ranges where they are."
            ),
        )
        logger.info(f"Deleted {deleted_count} ranges for event {event.name}")

    event.status = EventStatus.COMPLETED
    db.commit()
    db.refresh(event)

    logger.info(
        f"Event completed: {event.name} (cleanup_ranges={cleanup_ranges}, deleted={deleted_count})"
    )
    return build_event_response(event, db)


@router.post("/{event_id}/cancel", response_model=EventResponse)
def cancel_event(
    event_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
    cleanup_ranges: bool = Query(True, description="Delete participant ranges on cancellation"),
):
    """Cancel an event.

    If cleanup_ranges=True (default), all participant ranges will be deleted.
    """
    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not can_manage_event(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized")

    # Delete participant ranges if requested
    deleted_count = 0
    if cleanup_ranges:
        deleted_count = _delete_event_ranges(
            event_id,
            db,
            outcome=(
                "The event was not cancelled. Retry, or cancel it with cleanup_ranges=false "
                "to cancel the event and leave those ranges where they are."
            ),
        )
        logger.info(f"Deleted {deleted_count} ranges for cancelled event {event.name}")

    event.status = EventStatus.CANCELLED
    db.commit()
    db.refresh(event)

    logger.info(
        f"Event cancelled: {event.name} (cleanup_ranges={cleanup_ranges}, deleted={deleted_count})"
    )
    return build_event_response(event, db)


@router.post("/{event_id}/reactivate", response_model=EventResponse)
def reactivate_event(
    event_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Reactivate a cancelled event, returning it to draft status.

    This allows cancelled events to be re-scheduled or started again.
    """
    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not can_manage_event(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized")

    if event.status != EventStatus.CANCELLED:
        raise HTTPException(status_code=400, detail="Only cancelled events can be reactivated")

    event.status = EventStatus.DRAFT
    db.commit()
    db.refresh(event)

    logger.info(f"Event reactivated: {event.name} by {current_user.username}")
    return build_event_response(event, db)


# ============ Participants ============


@router.get("/{event_id}/participants", response_model=List[EventParticipantResponse])
def list_participants(
    event_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """List event participants with their assigned range info."""
    from proving_ground.models.range import Range

    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not can_view_event(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized")

    participants = db.query(EventParticipant).filter(EventParticipant.event_id == event_id).all()

    results = []
    for p in participants:
        user = db.query(User).filter(User.id == p.user_id).first()

        # Get range info if assigned
        range_status = None
        range_name = None
        if p.range_id:
            range_obj = db.query(Range).filter(Range.id == p.range_id).first()
            if range_obj:
                range_status = range_obj.status.value
                range_name = range_obj.name

        results.append(
            EventParticipantResponse(
                id=p.id,
                event_id=p.event_id,
                user_id=p.user_id,
                role=p.role,
                is_confirmed=p.is_confirmed,
                created_at=p.created_at,
                username=user.username if user else None,
                range_id=p.range_id,
                range_status=range_status,
                range_name=range_name,
            )
        )

    return results


@router.post("/{event_id}/participants", response_model=EventParticipantResponse)
def add_participant(
    event_id: UUID,
    data: EventParticipantCreate,
    db: DBSession,
    current_user: CurrentUser,
):
    """Add a participant to an event.

    A student joining a RUNNING event with a blueprint is given a lab the same way the start
    would have given them one: the cohort's shared range if the event is a team exercise, or a
    range of their own if it is self-paced.
    """
    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not can_manage_event(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized")

    # Check if user exists
    user = db.query(User).filter(User.id == data.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if already a participant
    existing = (
        db.query(EventParticipant)
        .filter(
            EventParticipant.event_id == event_id,
            EventParticipant.user_id == data.user_id,
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="User is already a participant")

    participant = EventParticipant(
        event_id=event_id,
        user_id=data.user_id,
        role=data.role.value,
        is_confirmed=True,
    )

    db.add(participant)
    db.flush()  # Get the participant ID before potential range creation

    # If event is RUNNING with a blueprint and this is a student, give them a lab
    deploy_id: str | None = None
    if (
        event.status == EventStatus.RUNNING
        and event.blueprint_id
        and data.role == ParticipantRole.STUDENT
    ):
        from proving_ground.api import kubernetes_ranges

        shared = cohort_range(event_id, db)
        if shared is not None:
            # A team exercise has one environment and this student joins it. Deploying a second
            # range for them would put a member of the team in a copy of the exercise instead of
            # in the exercise, and the rest of the cohort would never see them.
            participant.range_id = shared.id
            logger.info(
                f"Late-joining student {user.username} joined the shared range '{shared.name}'"
            )
        else:
            # Refused rather than logged. This used to swallow every failure and add the student
            # anyway, so an instructor adding someone to a running class got a participant with
            # no lab and no reason -- and on an Era B blueprint that was every time, because the
            # Era A parser this called cannot read one. Nothing is committed yet, so a refusal
            # leaves the event exactly as it was.
            blueprint, blueprint_config = _readable_blueprint(db, event)
            range_obj = _range_from_blueprint(
                db,
                blueprint=blueprint,
                blueprint_config=blueprint_config,
                name=f"{event.name} - {user.username}",
                created_by=current_user.id,
            )
            range_obj.assigned_to_user_id = participant.user_id
            range_obj.training_event_id = event.id
            participant.range_id = range_obj.id
            db.flush()
            if kubernetes_ranges.is_kubernetes():
                kubernetes_ranges.validate_for_deploy(db, range_obj.id)
            deploy_id = str(range_obj.id)

    db.commit()
    db.refresh(participant)

    # After the commit, for the same reason the start path queues after its own: the worker
    # reads the range by id in a session of its own and cannot see an uncommitted row.
    if deploy_id:
        from proving_ground.tasks.deployment import deploy_range_task

        deploy_range_task.send(deploy_id)
        logger.info(f"Queued the lab for late-joining student {user.username}")

    return EventParticipantResponse(
        id=participant.id,
        event_id=participant.event_id,
        user_id=participant.user_id,
        role=participant.role,
        is_confirmed=participant.is_confirmed,
        created_at=participant.created_at,
        username=user.username,
        range_id=participant.range_id,
    )


@router.post("/{event_id}/join", response_model=EventParticipantResponse)
def join_event(
    event_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
    role: ParticipantRole = Query(ParticipantRole.STUDENT, description="Role to join as"),
):
    """Join an event as a participant (self-registration).

    Self-registration means the student role. The event role decides what the briefing serves,
    so a free choice here was a way to be served the instructor notes: anyone who could see an
    event could join it as an instructor and read the answers to it. Someone who may already
    manage the event may still pick, because they can already see everything in it.
    """
    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not can_view_event(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized to join this event")

    if role is not ParticipantRole.STUDENT and not can_manage_event(event, current_user):
        raise HTTPException(
            status_code=403,
            detail=(
                f"You can join this event as a student. Being added as a {role.value} is the "
                "instructor's to decide, because it changes what the briefing shows you."
            ),
        )

    # Check if already a participant
    existing = (
        db.query(EventParticipant)
        .filter(
            EventParticipant.event_id == event_id,
            EventParticipant.user_id == current_user.id,
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="Already joined this event")

    participant = EventParticipant(
        event_id=event_id,
        user_id=current_user.id,
        role=role.value,
        is_confirmed=True,
    )

    db.add(participant)
    db.commit()
    db.refresh(participant)

    return EventParticipantResponse(
        id=participant.id,
        event_id=participant.event_id,
        user_id=participant.user_id,
        role=participant.role,
        is_confirmed=participant.is_confirmed,
        created_at=participant.created_at,
        username=current_user.username,
    )


@router.delete("/{event_id}/participants/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_participant(
    event_id: UUID,
    user_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Remove a participant from an event."""
    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # Can remove if: admin, event owner, or self
    if not (can_manage_event(event, current_user) or user_id == current_user.id):
        raise HTTPException(status_code=403, detail="Not authorized")

    participant = (
        db.query(EventParticipant)
        .filter(
            EventParticipant.event_id == event_id,
            EventParticipant.user_id == user_id,
        )
        .first()
    )
    if not participant:
        raise HTTPException(status_code=404, detail="Participant not found")

    db.delete(participant)
    db.commit()


# ============ Role-Based Content Delivery ============


@router.get("/{event_id}/briefing", response_model=EventBriefingResponse)
def get_event_briefing(
    event_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Get briefing content for an event, filtered by user's role."""
    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not can_view_event(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized")

    # Get user's role in this event
    user_role = get_user_event_role(event, current_user, db)

    # Get content items
    content_items = []
    for content_id_str in event.content_ids:
        try:
            content_uuid = UUID(content_id_str)
            content = db.query(Content).filter(Content.id == content_uuid).first()
            if content and content.is_published:
                # Role-based content filtering
                # Students see: student_guide, reference_material
                # Instructors see: all
                # Evaluators see: all except instructor_notes
                content_type = content.content_type.value

                should_include = False
                if user_role == ParticipantRole.INSTRUCTOR:
                    should_include = True
                elif user_role == ParticipantRole.EVALUATOR:
                    should_include = content_type != "instructor_notes"
                elif user_role == ParticipantRole.STUDENT:
                    should_include = content_type in [
                        "student_guide",
                        "reference_material",
                        "custom",
                    ]
                else:  # observer
                    should_include = content_type in ["student_guide", "reference_material"]

                if should_include:
                    content_items.append(
                        EventContentItem(
                            id=content.id,
                            title=content.title,
                            description=content.description,
                            content_type=content_type,
                            body_html=content.body_html,
                            version=content.version,
                        )
                    )
        except ValueError:
            continue

    # Get range status if linked
    range_status = None
    if event.range_id:
        from proving_ground.models.range import Range

        range_obj = db.query(Range).filter(Range.id == event.range_id).first()
        if range_obj:
            range_status = range_obj.status.value

    return EventBriefingResponse(
        event_id=event.id,
        event_name=event.name,
        user_role=user_role,
        content_items=content_items,
        range_id=event.range_id,
        range_status=range_status,
    )


# ============ VM Console Visibility Control ============


@router.get("/{event_id}/participants/{user_id}/vm-visibility", response_model=VMVisibilityResponse)
def get_participant_vm_visibility(
    event_id: UUID,
    user_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Get VM visibility settings for a participant."""
    from proving_ground.models.vm import VM

    # Validate event exists
    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # Check authorization - must be able to manage event or be the participant
    if not can_manage_event(event, current_user) and current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Find participant
    participant = (
        db.query(EventParticipant)
        .filter(
            EventParticipant.event_id == event_id,
            EventParticipant.user_id == user_id,
        )
        .first()
    )
    if not participant:
        raise HTTPException(status_code=404, detail="Participant not found")

    # Get user info
    user = db.query(User).filter(User.id == user_id).first()
    username = user.username if user else "Unknown"

    # Get VMs from participant's range
    vms_list = []
    if participant.range_id:
        vms = db.query(VM).filter(VM.range_id == participant.range_id).all()
        hidden_ids = set(participant.hidden_vm_ids or [])
        for vm in vms:
            vms_list.append(
                VMVisibilityVM(
                    id=vm.id,
                    hostname=vm.hostname,
                    status=vm.status.value if hasattr(vm.status, "value") else str(vm.status),
                    is_hidden=vm.id in hidden_ids,
                )
            )

    return VMVisibilityResponse(
        participant_id=participant.id,
        user_id=participant.user_id,
        username=username,
        range_id=participant.range_id,
        hidden_vm_ids=participant.hidden_vm_ids or [],
        vms=vms_list,
    )


@router.put("/{event_id}/participants/{user_id}/vm-visibility", response_model=VMVisibilityResponse)
def update_participant_vm_visibility(
    event_id: UUID,
    user_id: UUID,
    data: VMVisibilityUpdate,
    db: DBSession,
    current_user: CurrentUser,
):
    """Update which VMs are hidden from a participant."""
    from proving_ground.models.vm import VM

    # Validate event exists
    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # Check authorization - must be able to manage event
    if not can_manage_event(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized to manage VM visibility")

    # Find participant
    participant = (
        db.query(EventParticipant)
        .filter(
            EventParticipant.event_id == event_id,
            EventParticipant.user_id == user_id,
        )
        .first()
    )
    if not participant:
        raise HTTPException(status_code=404, detail="Participant not found")

    # Update hidden VM IDs
    participant.hidden_vm_ids = [str(vm_id) for vm_id in data.hidden_vm_ids]
    db.commit()
    db.refresh(participant)

    # Get user info
    user = db.query(User).filter(User.id == user_id).first()
    username = user.username if user else "Unknown"

    # Get VMs from participant's range
    vms_list = []
    if participant.range_id:
        vms = db.query(VM).filter(VM.range_id == participant.range_id).all()
        hidden_ids = set(participant.hidden_vm_ids or [])
        for vm in vms:
            vms_list.append(
                VMVisibilityVM(
                    id=vm.id,
                    hostname=vm.hostname,
                    status=vm.status.value if hasattr(vm.status, "value") else str(vm.status),
                    is_hidden=str(vm.id) in hidden_ids,
                )
            )

    logger.info(
        f"Updated VM visibility for participant {user_id} in event {event_id}: hidden={data.hidden_vm_ids}"
    )

    return VMVisibilityResponse(
        participant_id=participant.id,
        user_id=participant.user_id,
        username=username,
        range_id=participant.range_id,
        hidden_vm_ids=(
            [UUID(vm_id) for vm_id in participant.hidden_vm_ids]
            if participant.hidden_vm_ids
            else []
        ),
        vms=vms_list,
    )


@router.put("/{event_id}/vm-visibility/bulk")
def bulk_update_vm_visibility(
    event_id: UUID,
    data: BulkVMVisibilityUpdate,
    db: DBSession,
    current_user: CurrentUser,
):
    """Apply visibility setting for a VM to all student participants."""
    # Validate event exists
    event = db.query(TrainingEvent).filter(TrainingEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    # Check authorization
    if not can_manage_event(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized")

    # Get all student participants
    participants = (
        db.query(EventParticipant)
        .filter(
            EventParticipant.event_id == event_id,
            EventParticipant.role == ParticipantRole.STUDENT.value,
        )
        .all()
    )

    vm_id_str = str(data.vm_id)
    updated_count = 0

    for participant in participants:
        hidden_ids = list(participant.hidden_vm_ids or [])

        if data.is_hidden:
            # Add to hidden list if not already there
            if vm_id_str not in hidden_ids:
                hidden_ids.append(vm_id_str)
                participant.hidden_vm_ids = hidden_ids
                updated_count += 1
        else:
            # Remove from hidden list if present
            if vm_id_str in hidden_ids:
                hidden_ids.remove(vm_id_str)
                participant.hidden_vm_ids = hidden_ids
                updated_count += 1

    db.commit()

    action = "hidden" if data.is_hidden else "shown"
    logger.info(
        f"Bulk updated VM {data.vm_id} visibility to {action} for {updated_count} participants in event {event_id}"
    )

    return {
        "message": f"VM {action} for {updated_count} participants",
        "vm_id": data.vm_id,
        "is_hidden": data.is_hidden,
        "updated_count": updated_count,
    }
