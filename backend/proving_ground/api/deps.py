# backend/proving_ground/api/deps.py
from typing import Annotated, List, Optional
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, and_, select

from proving_ground.database import get_db
from proving_ground.models.user import User, UserRole
from proving_ground.models.resource_tag import ResourceTag
from proving_ground.models.event import EventParticipant
from proving_ground.models.range import Range
from proving_ground.utils.security import decode_access_token

# auto_error=False so a *missing* Authorization header reaches us instead of
# FastAPI raising 403 on our behalf.
#
# HTTPBearer's default returns 403 Forbidden when the header is absent, which is
# wrong twice over. RFC 7235 reserves 401 for "no or bad credentials" and 403 for
# "authenticated but not permitted", and every other auth failure below already
# raises 401. More concretely: the frontend's response interceptor only redirects
# to login on 401, so a user whose token was missing entirely — cleared storage,
# fresh browser — got a 403 the client ignored and a broken page instead of a
# login prompt. An invalid token logged them out correctly; no token at all did
# not.
security = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(security)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """Extract and validate user from JWT token, loading attributes."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    user_id = decode_access_token(token)

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Eagerly load attributes to avoid N+1 queries
    user = db.query(User).options(joinedload(User.attributes)).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated",
        )

    return user


# Legacy role-based check (for backwards compatibility)
def require_role(*roles: UserRole):
    """Legacy role checker using old role enum field."""

    def role_checker(current_user: Annotated[User, Depends(get_current_user)]) -> User:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required role: {', '.join(r.value for r in roles)}",
            )
        return current_user

    return role_checker


# ABAC-based authorization checks
def require_any_role(*role_values: str):
    """
    Require user to have at least one of the specified roles (ABAC).
    Uses the new attributes system.
    """

    def checker(current_user: Annotated[User, Depends(get_current_user)]) -> User:
        if not current_user.has_any_role(*role_values):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required role: {', '.join(role_values)}",
            )
        return current_user

    return checker


def require_admin():
    """Require user to have admin role."""

    def checker(current_user: Annotated[User, Depends(get_current_user)]) -> User:
        if not current_user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Administrator access required",
            )
        return current_user

    return checker


def require_any_tag(*tags: str):
    """Require user to have at least one of the specified tags."""

    def checker(current_user: Annotated[User, Depends(get_current_user)]) -> User:
        if not current_user.has_any_tag(*tags):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required tag: {', '.join(tags)}",
            )
        return current_user

    return checker


def get_student_accessible_range_ids(user_id: UUID, db: Session) -> List[UUID]:
    """
    Get range IDs accessible to a student via:
    1. Direct assignment (Range.assigned_to_user_id)
    2. Event participation (EventParticipant.range_id)

    Returns:
        List of range UUIDs the student can access
    """
    accessible_ids = set()

    # 1. Ranges directly assigned to user
    direct_ranges = db.query(Range.id).filter(Range.assigned_to_user_id == user_id).all()
    accessible_ids.update(r.id for r in direct_ranges)

    # 2. Ranges assigned via event participation
    participant_ranges = (
        db.query(EventParticipant.range_id)
        .filter(EventParticipant.user_id == user_id, EventParticipant.range_id.isnot(None))
        .all()
    )
    accessible_ids.update(r.range_id for r in participant_ranges if r.range_id)

    return list(accessible_ids)


def is_student_only(user: User) -> bool:
    """
    Check if user has ONLY the student role (and no elevated roles).
    Students get assignment-based visibility instead of tag-based.
    """
    user_roles = set(user.roles)
    elevated_roles = {"admin", "engineer", "evaluator", "white_cell"}
    return user_roles == {"student"} or (
        not user_roles.intersection(elevated_roles) and "student" in user_roles
    )


def filter_by_visibility(query, resource_type: str, current_user: User, db: Session, model_class):
    """
    Filter query to only return resources visible to the user based on tags.

    Visibility rules:
    1. Admins can see ALL resources
    2. Students (with no elevated roles) see ONLY assigned resources
    3. Other non-admin users see:
       - Resources they own
       - Resources with NO tags (public)
       - Resources with at least one matching tag

    Args:
        query: SQLAlchemy query object
        resource_type: Type of resource ('range', 'template', 'artifact')
        current_user: Current authenticated user
        db: Database session
        model_class: The SQLAlchemy model class (Range, BaseImage, Artifact)

    Returns:
        Filtered query
    """
    # Admins see everything
    if current_user.is_admin:
        return query

    # Students see only assigned resources (for ranges only)
    if resource_type == "range" and is_student_only(current_user):
        accessible_ids = get_student_accessible_range_ids(current_user.id, db)
        if not accessible_ids:
            # No assignments - return empty result
            return query.filter(model_class.id == None)
        return query.filter(model_class.id.in_(accessible_ids))

    # Ranges carry an explicit visibility. Everything below this is the older
    # tag model, which is still right for templates and artifacts but was never
    # right for ranges: it treats "has no tags" as "public", so every range
    # anyone created was visible to every non-student account.
    if resource_type == "range":
        return _filter_ranges_by_visibility(query, current_user, db, model_class)

    # Tag-based visibility for other users/resources
    user_tags = current_user.tags

    # Get all resource IDs that have ANY tags
    tagged_resource_ids = (
        db.query(ResourceTag.resource_id)
        .filter(ResourceTag.resource_type == resource_type)
        .distinct()
        .subquery()
    )

    if not user_tags:
        # User has no tags - only see untagged resources
        return query.filter(~model_class.id.in_(tagged_resource_ids))

    # User has tags - get resources matching their tags
    matching_resource_ids = (
        db.query(ResourceTag.resource_id)
        .filter(ResourceTag.resource_type == resource_type, ResourceTag.tag.in_(user_tags))
        .distinct()
        .subquery()
    )

    # Return: untagged resources OR resources matching user's tags
    return query.filter(
        or_(
            ~model_class.id.in_(tagged_resource_ids),  # Untagged (public)
            model_class.id.in_(matching_resource_ids),  # Matching tags
        )
    )


def _filter_ranges_by_visibility(query, current_user: User, db: Session, model_class):
    """Ranges a non-admin, non-student user may see.

    Owned, public, or shared with them -- by name or by a tag they hold. A
    PRIVATE range belonging to someone else is not in the list at all, which is
    the point: it should not be visible, not merely unopenable.
    """
    from proving_ground.models.range import RangeShare, RangeVisibility

    shared_with_me = (
        db.query(RangeShare.range_id).filter(RangeShare.user_id == current_user.id).subquery()
    )

    conditions = [
        model_class.created_by == current_user.id,
        model_class.visibility == RangeVisibility.PUBLIC,
        and_(
            model_class.visibility == RangeVisibility.SHARED,
            model_class.id.in_(select(shared_with_me.c.range_id)),
        ),
    ]

    # A tag grants sight only of a SHARED range. Holding a tag must not surface
    # something its owner marked private.
    user_tags = current_user.tags
    if user_tags:
        tagged = (
            db.query(ResourceTag.resource_id)
            .filter(ResourceTag.resource_type == "range", ResourceTag.tag.in_(user_tags))
            .distinct()
            .subquery()
        )
        conditions.append(
            and_(
                model_class.visibility == RangeVisibility.SHARED,
                model_class.id.in_(select(tagged.c.resource_id)),
            )
        )

    return query.filter(or_(*conditions))


def check_resource_access(
    resource_type: str, resource_id: UUID, current_user: User, db: Session, owner_id: UUID = None
) -> bool:
    """
    Check if user can access a specific resource.

    Access granted if:
    1. User is admin
    2. User is the owner (if owner_id provided)
    3. For ranges: student is assigned (directly or via event)
    4. Resource has no tags (public)
    5. User has at least one matching tag

    Returns True if access is granted, raises HTTPException otherwise.
    """
    # Admins always have access
    if current_user.is_admin:
        return True

    # Owners always have access to their own resources
    if owner_id and owner_id == current_user.id:
        return True

    # For ranges: check if student is assigned
    if resource_type == "range" and is_student_only(current_user):
        accessible_ids = get_student_accessible_range_ids(current_user.id, db)
        if resource_id in accessible_ids:
            return True
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not assigned to this lab",
        )

    # Ranges answer to their own visibility, not to "untagged means public".
    # Without this the list would hide a private range while a direct GET by id
    # still returned it -- hidden rather than protected.
    if resource_type == "range":
        from proving_ground.models.range import Range as _Range, RangeShare, RangeVisibility

        range_obj = db.query(_Range).filter(_Range.id == resource_id).first()
        if range_obj is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Range not found",
            )

        if range_obj.visibility == RangeVisibility.PUBLIC:
            return True

        if range_obj.visibility == RangeVisibility.SHARED:
            shared = (
                db.query(RangeShare)
                .filter(
                    RangeShare.range_id == resource_id,
                    RangeShare.user_id == current_user.id,
                )
                .first()
            )
            if shared:
                return True
            tags = [
                t.tag
                for t in db.query(ResourceTag.tag)
                .filter(
                    ResourceTag.resource_type == "range",
                    ResourceTag.resource_id == resource_id,
                )
                .all()
            ]
            if tags and current_user.has_any_tag(*tags):
                return True

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have access to this range",
        )

    # Check resource tags (for non-student users or non-range resources)
    resource_tags = (
        db.query(ResourceTag.tag)
        .filter(ResourceTag.resource_type == resource_type, ResourceTag.resource_id == resource_id)
        .all()
    )

    tag_values = [t.tag for t in resource_tags]

    # No tags = public
    if not tag_values:
        return True

    # Check for matching tags
    if current_user.has_any_tag(*tag_values):
        return True

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You don't have access to this resource",
    )


def check_resource_control(
    resource_type: str, resource_id: UUID, current_user: User, db: Session, owner_id: UUID = None
) -> bool:
    """Check if a user may CHANGE a resource, not merely see it.

    Deliberately stricter than check_resource_access, and the difference is the
    point of this function existing.

    That one is a visibility model: it grants access to an untagged resource
    because "no tags = public", and to anyone holding a matching tag. Both are
    reasonable answers to "may I look at this". Neither is a reasonable answer
    to "may I tear this down" -- under it, one engineer could delete another
    engineer's untagged range, and a tag meant to share a range for viewing
    would also hand over the power to destroy it.

    Control is therefore owner-or-admin. A student never controls a range: an
    assignment is permission to use a lab, not to delete it.

    Returns True, or raises 403. Never returns False, so a caller cannot get
    the check wrong by ignoring the result.
    """
    if current_user.is_admin:
        return True

    if owner_id and owner_id == current_user.id:
        return True

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"You do not own this {resource_type}",
    )


def _load_range(range_id: UUID, db: Session) -> Range:
    range_obj = db.query(Range).filter(Range.id == range_id).first()
    if not range_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Range not found",
        )
    return range_obj


def check_range_control(range_id: UUID, current_user: User, db: Session) -> Range:
    """Require control of the range a resource belongs to. Returns the range.

    Networks and VMs have no owner of their own -- they belong to a range, and
    whoever controls the range controls them. Deleting someone else's network
    or tearing down their VM is destroying their range a piece at a time, so it
    answers to the same rule.

    This exists so the check is one line at each of roughly thirty call sites
    rather than a repeated fetch-then-check that only some of them remember.
    """
    range_obj = _load_range(range_id, db)
    check_resource_control("range", range_obj.id, current_user, db, range_obj.created_by)
    return range_obj


def check_range_access(range_id: UUID, current_user: User, db: Session) -> Range:
    """Require read access to the range a resource belongs to.

    The visibility half: a student assigned to a lab can see its networks and
    VMs, and an untagged range stays readable, exactly as for the range itself.
    """
    range_obj = _load_range(range_id, db)
    check_resource_access("range", range_obj.id, current_user, db, range_obj.created_by)
    return range_obj


# Type aliases for common dependencies
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin())]
DBSession = Annotated[Session, Depends(get_db)]


# Download endpoints used to take the session token in a query string, so a browser could be
# navigated straight at them. Nothing does that: every caller goes through the app's axios
# instance, which sets `Authorization: Bearer` on the way out. What the query parameter bought
# was a full API credential in browser history and in every access log between the browser and
# here -- the same defect the websocket ticket exists to close, through a different door. A
# download that genuinely cannot set a header wants a ticket like the console's, not the session.
DownloadUser = Annotated[User, Depends(get_current_user)]
