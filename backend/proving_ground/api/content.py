# backend/proving_ground/api/content.py
"""Content API endpoints for training materials."""

import hashlib
import logging
import markdown
from datetime import datetime, timezone
from typing import Annotated, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, status
from fastapi.responses import Response
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from proving_ground.api.deps import get_current_user, get_db, require_any_role

# A Knowledge Check lives inside a Content row's walkthrough_data, so it leaves
# the platform through this module as well as through api/walkthrough.py. One
# function removes the answer key for both. It is defined beside the
# learner-facing delivery route because that is where it is load-bearing;
# api/msel.py imports it from there for the same reason.
from proving_ground.api.walkthrough import strip_quiz_answers
from proving_ground.services.object_store import content_bucket
from proving_ground.models.user import User
from proving_ground.models.content import Content, ContentAsset, ContentType
from proving_ground.schemas.content import (
    ContentCreate,
    ContentUpdate,
    ContentResponse,
    ContentListResponse,
    ContentAssetResponse,
    ContentExport,
    ContentImport,
)
from proving_ground.models.catalog import CatalogInstalledItem
from proving_ground.schemas.content_bundle import (
    BundleConflict,
    BundleDecision,
    BundleImportResult,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/content", tags=["content"])

# Type aliases
DBSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


# ============ Authorization ============
#
# Content is not a range, so none of the three range checks in api/deps.py fit
# it: there is no tag model over content, no assignment, and no console. What
# decides a read here is the author's own act of publishing, and what decides a
# write is whether the caller writes content at all.

# Content written for the people running the exercise, never handed to a
# learner -- not even once it is published, because publishing releases a
# document to its intended audience and these two were never written for the
# learner's audience.
INSTRUCTOR_ONLY_TYPES = frozenset({ContentType.INSTRUCTOR_NOTES, ContentType.MSEL})

# There is no separate "author" role in the attribute model (see
# models/user.py AVAILABLE_ROLES): admin and engineer are the roles that write
# training material. An evaluator reviews an exercise rather than writing one,
# and a student is the audience, so neither creates content.
AUTHORING_ROLES = ("admin", "engineer")

# Who the Content Library is for. The same three roles the browser lets into
# /content, and deliberately so -- an evaluator whose job is to judge how an
# exercise ran needs the instructor notes and the MSEL, and gating them out
# would leave them on a page with almost nothing on it.
LIBRARY_ROLES = ("admin", "engineer", "evaluator")

# Declared on the route rather than called inside it, the way AdminUser already
# is across admin.py, catalog.py and cache.py: a guard in the signature is
# visible in the OpenAPI schema and cannot be skipped by an early return.
AuthorUser = Annotated[User, Depends(require_any_role(*AUTHORING_ROLES))]


def may_author(user: User) -> bool:
    """True when the caller writes training material."""
    return user.is_admin or user.has_any_role(*AUTHORING_ROLES)


def may_read_everything(user: User) -> bool:
    """True when the caller works in the library, so drafts are not secrets."""
    return user.is_admin or user.has_any_role(*LIBRARY_ROLES)


def may_read_content(content: Content, user: User) -> bool:
    """True when this caller may read this document.

    Someone who works in the library sees everything, including a colleague's
    draft -- writing a course is collaborative and a draft is not a secret
    from the people producing it. Anyone else sees a document only once its
    author published it, and never the instructor-facing types.
    """
    if content.created_by_id == user.id or may_read_everything(user):
        return True
    return bool(content.is_published) and content.content_type not in INSTRUCTOR_ONLY_TYPES


def may_read_authored_form(content: Content, user: User) -> bool:
    """True when the caller may see the document as its author wrote it.

    Everyone else reaches a document only because it was published to the
    learner's audience, and the authored form carries two things that audience
    must not have: the Knowledge Check answer key, and the bundle that would
    carry it back out. This is the unconditional half of may_read_content --
    the branch that grants a read without asking whether the document was
    published.
    """
    return content.created_by_id == user.id or may_read_everything(user)


def _require_read(content: Content, user: User) -> None:
    if may_read_content(content, user):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="This content is unpublished or written for instructors",
    )


def _delivered(content: Content, user: User) -> ContentResponse:
    """The document as this caller may have it.

    A published student guide is readable by the learner it was written for,
    and the range it is attached to hands out its id, so the Content API is a
    second way to the same walkthrough that api/walkthrough.py delivers. It
    has to withhold the same answer key, or stripping it there only moves the
    leak one route sideways.
    """
    response = ContentResponse.model_validate(content)
    if may_read_authored_form(content, user):
        return response
    return response.model_copy(
        update={"walkthrough_data": strip_quiz_answers(content.walkthrough_data, None)}
    )


def _require_owner_or_admin(content: Content, user: User) -> None:
    """Changing a document is its owner's call, or an admin's.

    Deliberately narrower than may_author: an engineer may read a colleague's
    draft without being able to rewrite, publish or delete it.
    """
    if content.created_by_id == user.id or user.is_admin:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Not authorized to change this content",
    )


def _readable_content_filter(user: User):
    """The SQL half of may_read_content, for list queries.

    Kept next to it so the two cannot drift: a document the list shows must be
    one a direct GET would also return.
    """
    return or_(
        Content.created_by_id == user.id,
        and_(
            Content.is_published.is_(True),
            Content.content_type.notin_(tuple(INSTRUCTOR_ONLY_TYPES)),
        ),
    )


def render_markdown_to_html(md_content: str) -> str:
    """Render markdown to HTML with extensions."""
    if not md_content:
        return ""
    extensions = [
        "markdown.extensions.fenced_code",
        "markdown.extensions.tables",
        "markdown.extensions.codehilite",
        "markdown.extensions.toc",
    ]
    return markdown.markdown(md_content, extensions=extensions)


def render_walkthrough_to_html(walkthrough_data: dict) -> str:
    """Render walkthrough data structure to HTML."""
    if not walkthrough_data:
        return ""

    html_parts = []

    # Walkthrough title (if different from content title)
    if walkthrough_data.get("title"):
        html_parts.append(f'<h2 class="walkthrough-title">{walkthrough_data["title"]}</h2>')

    # Render each phase
    phases = walkthrough_data.get("phases", [])
    for phase_idx, phase in enumerate(phases, 1):
        phase_name = phase.get("name", f"Phase {phase_idx}")
        html_parts.append('<div class="phase">')
        html_parts.append(f'<h3 class="phase-name">{phase_name}</h3>')

        # Render each step in the phase
        steps = phase.get("steps", [])
        for step_idx, step in enumerate(steps, 1):
            step_title = step.get("title", f"Step {step_idx}")
            step_content = step.get("content", "")
            step_vm = step.get("vm")
            step_hints = step.get("hints", [])

            html_parts.append('<div class="step">')
            html_parts.append(f'<h4 class="step-title">{step_idx}. {step_title}</h4>')

            if step_vm:
                html_parts.append(f'<p class="step-vm"><strong>Target VM:</strong> {step_vm}</p>')

            # Render step content as markdown
            if step_content:
                rendered_content = render_markdown_to_html(step_content)
                html_parts.append(f'<div class="step-content">{rendered_content}</div>')

            # Render hints if present
            if step_hints:
                html_parts.append('<div class="hints">')
                html_parts.append('<p class="hints-label"><strong>Hints:</strong></p>')
                html_parts.append('<ul class="hints-list">')
                for hint in step_hints:
                    html_parts.append(f"<li>{hint}</li>")
                html_parts.append("</ul>")
                html_parts.append("</div>")

            html_parts.append("</div>")  # .step

        html_parts.append("</div>")  # .phase

    return "\n".join(html_parts)


# ============ Content CRUD ============


@router.post("", response_model=ContentResponse, status_code=status.HTTP_201_CREATED)
def create_content(
    data: ContentCreate,
    db: DBSession,
    current_user: AuthorUser,
):
    """Create new content."""
    # Render HTML from markdown
    body_html = render_markdown_to_html(data.body_markdown) if data.body_markdown else None

    # Convert walkthrough_data to dict if provided
    walkthrough_dict = None
    if data.walkthrough_data:
        walkthrough_dict = data.walkthrough_data.model_dump()

    content = Content(
        title=data.title,
        description=data.description,
        content_type=data.content_type,
        body_markdown=data.body_markdown,
        body_html=body_html,
        walkthrough_data=walkthrough_dict,
        tags=data.tags,
        organization=data.organization,
        created_by_id=current_user.id,
    )

    db.add(content)
    db.commit()
    db.refresh(content)

    logger.info(f"Content created: {content.title} by {current_user.username}")
    return content


@router.get("", response_model=List[ContentListResponse])
def list_content(
    db: DBSession,
    current_user: CurrentUser,
    # Annotated, not `= Query(...)`: with the older style the Python default is
    # the Query object itself, which is truthy, so a caller that is not FastAPI
    # -- a test, or another route -- silently filters on garbage.
    content_type: Annotated[
        Optional[ContentType], Query(description="Filter by content type")
    ] = None,
    tag: Annotated[Optional[str], Query(description="Filter by tag")] = None,
    search: Annotated[Optional[str], Query(description="Search in title and description")] = None,
    published_only: Annotated[bool, Query(description="Only show published content")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """List all content with optional filters."""
    query = db.query(Content)

    # Visibility first, so no later filter can widen it.
    if not may_read_everything(current_user):
        query = query.filter(_readable_content_filter(current_user))

    # Filter by content type
    if content_type:
        query = query.filter(Content.content_type == content_type)

    # Filter by tag
    if tag:
        query = query.filter(Content.tags.contains([tag]))

    # Search
    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            or_(
                Content.title.ilike(search_pattern),
                Content.description.ilike(search_pattern),
            )
        )

    # Published filter
    if published_only:
        query = query.filter(Content.is_published == True)

    # Order and paginate
    query = query.order_by(Content.updated_at.desc())
    query.count()
    content_list = query.offset(offset).limit(limit).all()

    return content_list


@router.get("/{content_id}", response_model=ContentResponse)
def get_content(
    content_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Get content by ID."""
    content = db.query(Content).filter(Content.id == content_id).first()
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    _require_read(content, current_user)
    return _delivered(content, current_user)


@router.put("/{content_id}", response_model=ContentResponse)
def update_content(
    content_id: UUID,
    data: ContentUpdate,
    db: DBSession,
    current_user: CurrentUser,
):
    """Update content."""
    content = db.query(Content).filter(Content.id == content_id).first()
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    _require_owner_or_admin(content, current_user)

    # Update fields
    update_data = data.model_dump(exclude_unset=True)

    # Re-render HTML if markdown changed
    if "body_markdown" in update_data:
        update_data["body_html"] = render_markdown_to_html(update_data["body_markdown"])

    # Convert walkthrough_data from Pydantic model to dict if needed
    if "walkthrough_data" in update_data and update_data["walkthrough_data"] is not None:
        # It's already a dict from model_dump, but ensure it's stored correctly
        pass

    for field, value in update_data.items():
        setattr(content, field, value)

    db.commit()
    db.refresh(content)

    logger.info(f"Content updated: {content.title} by {current_user.username}")
    return content


@router.delete("/{content_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_content(
    content_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Delete content."""
    content = db.query(Content).filter(Content.id == content_id).first()
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    _require_owner_or_admin(content, current_user)

    # Clean up catalog installed item record if this content was installed from catalog
    db.query(CatalogInstalledItem).filter(
        CatalogInstalledItem.local_resource_id == content_id
    ).delete()

    db.delete(content)
    db.commit()

    logger.info(f"Content deleted: {content.title} by {current_user.username}")


# ============ Publishing ============


@router.post("/{content_id}/publish", response_model=ContentResponse)
def publish_content(
    content_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Publish content."""
    content = db.query(Content).filter(Content.id == content_id).first()
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    _require_owner_or_admin(content, current_user)

    content.is_published = True
    db.commit()
    db.refresh(content)

    return content


@router.post("/{content_id}/unpublish", response_model=ContentResponse)
def unpublish_content(
    content_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Unpublish content."""
    content = db.query(Content).filter(Content.id == content_id).first()
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    _require_owner_or_admin(content, current_user)

    content.is_published = False
    db.commit()
    db.refresh(content)

    return content


# ============ Versioning ============


@router.post("/{content_id}/version", response_model=ContentResponse)
def create_content_version(
    content_id: UUID,
    db: DBSession,
    current_user: AuthorUser,
    new_version: str = Query(..., description="New version string (e.g., '1.1', '2.0')"),
):
    """Create a new version of content (duplicates with new version)."""
    content = db.query(Content).filter(Content.id == content_id).first()
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    # The role check is in the signature; this is the other half. The route
    # copies the body into a row the caller owns, so without a read check it
    # is a way to lift a draft out from behind the read rule and then read it
    # back as your own.
    _require_read(content, current_user)

    # Create new content as a copy
    new_content = Content(
        title=content.title,
        description=content.description,
        content_type=content.content_type,
        body_markdown=content.body_markdown,
        body_html=content.body_html,
        version=new_version,
        tags=content.tags.copy() if content.tags else [],
        organization=content.organization,
        created_by_id=current_user.id,
        is_published=False,
    )

    db.add(new_content)
    db.commit()
    db.refresh(new_content)

    logger.info(f"Content version created: {new_content.title} v{new_version}")
    return new_content


# ============ Export/Import ============


@router.get("/{content_id}/export")
def export_content(
    content_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
    format: Annotated[str, Query(description="Export format: json, md, html")] = "json",
):
    """Export content in various formats."""
    content = db.query(Content).filter(Content.id == content_id).first()
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    _require_read(content, current_user)

    if format == "md":
        # Return raw markdown
        return Response(
            content=content.body_markdown,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{content.title}.md"'},
        )
    elif format == "html":
        # Render content - check for walkthrough data first, then fall back to body_markdown
        if content.walkthrough_data:
            # Render walkthrough structure
            html_body = render_walkthrough_to_html(content.walkthrough_data)
            # Also include body_markdown if present (for additional notes)
            if content.body_markdown:
                html_body += '<hr><div class="additional-content">'
                html_body += render_markdown_to_html(content.body_markdown)
                html_body += "</div>"
        else:
            # Regular markdown content
            html_body = content.body_html or render_markdown_to_html(content.body_markdown or "")

        # Description as subtitle if present
        description_html = ""
        if content.description:
            description_html = f'<p class="description">{content.description}</p>'

        full_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{content.title}</title>
    <style>
        body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 0 auto; padding: 2rem; line-height: 1.6; }}
        h1 {{ border-bottom: 2px solid #333; padding-bottom: 0.5rem; }}
        .description {{ color: #666; font-style: italic; margin-bottom: 2rem; }}
        .phase {{ margin: 2rem 0; padding: 1rem; background: #f9f9f9; border-radius: 8px; }}
        .phase-name {{ color: #2563eb; margin-top: 0; }}
        .step {{ margin: 1.5rem 0; padding: 1rem; background: white; border-left: 4px solid #2563eb; }}
        .step-title {{ margin: 0 0 0.5rem 0; color: #1e40af; }}
        .step-vm {{ color: #666; font-size: 0.9rem; margin: 0.5rem 0; }}
        .step-content {{ margin: 1rem 0; }}
        .hints {{ background: #fef3c7; padding: 0.75rem; border-radius: 4px; margin-top: 1rem; }}
        .hints-label {{ margin: 0 0 0.5rem 0; }}
        .hints-list {{ margin: 0; padding-left: 1.5rem; }}
        pre {{ background: #1e293b; color: #e2e8f0; padding: 1rem; overflow-x: auto; border-radius: 4px; }}
        code {{ background: #e2e8f0; padding: 0.2rem 0.4rem; border-radius: 3px; font-size: 0.9em; }}
        pre code {{ background: none; padding: 0; }}
        table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
        th, td {{ border: 1px solid #ddd; padding: 0.75rem; text-align: left; }}
        th {{ background: #f3f4f6; }}
        hr {{ margin: 2rem 0; border: none; border-top: 1px solid #e5e7eb; }}
        .meta {{ color: #666; font-size: 0.85rem; margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #e5e7eb; }}
    </style>
</head>
<body>
<h1>{content.title}</h1>
{description_html}
{html_body}
<div class="meta">
    <p>Exported from PROVING GROUND &bull; Version {content.version} &bull; Type: {content.content_type.value}</p>
</div>
</body>
</html>"""
        return Response(
            content=full_html,
            media_type="text/html",
            headers={"Content-Disposition": f'attachment; filename="{content.title}.html"'},
        )
    else:
        # Return JSON with metadata
        export_data = ContentExport(
            title=content.title,
            description=content.description,
            content_type=content.content_type,
            body_markdown=content.body_markdown,
            version=content.version,
            tags=content.tags or [],
            organization=content.organization,
            exported_at=datetime.now(timezone.utc),
        )
        return export_data


@router.post("/import", response_model=ContentResponse)
def import_content(
    data: ContentImport,
    db: DBSession,
    current_user: AuthorUser,
):
    """Import content from JSON."""
    body_html = render_markdown_to_html(data.body_markdown) if data.body_markdown else None

    content = Content(
        title=data.title,
        description=data.description,
        content_type=data.content_type,
        body_markdown=data.body_markdown,
        body_html=body_html,
        version=data.version or "1.0",
        tags=data.tags or [],
        organization=data.organization,
        created_by_id=current_user.id,
    )

    db.add(content)
    db.commit()
    db.refresh(content)

    logger.info(f"Content imported: {content.title} by {current_user.username}")
    return content


# ============ Git-native bundle (PG-104) ============


def _minio():
    from proving_ground.services.object_store import object_client

    return object_client()


def _load_asset_bytes(asset: ContentAsset) -> Optional[bytes]:
    """Fetch an asset's bytes, or None if object storage cannot produce them.

    Returning None rather than raising is deliberate: a bundle that lists its
    assets honestly is more useful than an export that fails outright because
    one image is missing.
    """
    parts = (asset.file_path or "").split("/", 1)
    if len(parts) != 2:
        logger.warning("asset %s has a malformed path %r", asset.id, asset.file_path)
        return None
    try:
        obj = _minio().get_object(parts[0], parts[1])
        try:
            return obj.read()
        finally:
            obj.close()
            obj.release_conn()
    except Exception as exc:  # noqa: BLE001 - any storage failure is the same answer here
        logger.warning("could not read asset %s: %s", asset.id, exc)
        return None


def _save_asset_bytes(content_id: UUID, filename: str, mime_type: str, data: bytes) -> str:
    from io import BytesIO

    client = _minio()
    bucket_name = content_bucket()
    if not client.bucket_exists(bucket_name):
        client.make_bucket(bucket_name)
    digest = hashlib.sha256(data).hexdigest()
    object_name = f"{content_id}/{digest[:8]}_{filename}"
    client.put_object(bucket_name, object_name, BytesIO(data), len(data), content_type=mime_type)
    return f"{bucket_name}/{object_name}"


def _decision_response(decision) -> BundleDecision:
    return BundleDecision(
        slug=decision.slug,
        title=decision.title,
        action=decision.action,
        content_id=decision.content_id,
        conflicts=[
            BundleConflict(
                field=c.field,
                local=c.local,
                incoming=c.incoming,
                summary=c.summary,
                description=c.describe(),
            )
            for c in decision.conflicts
        ],
        reason=decision.reason,
        writes=decision.writes,
    )


async def _read_bundle_upload(file: UploadFile):
    """Read an uploaded bundle archive into a parsed bundle."""
    from proving_ground.content.archive import ArchiveError, unpack
    from proving_ground.content.bundle import BundleError, read_bundle

    payload = await file.read()
    try:
        return read_bundle(unpack(payload))
    except (ArchiveError, BundleError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _plan(db: Session, bundle, overwrite: bool):
    from proving_ground.content.importer import plan_import
    from proving_ground.content.store import find_local

    local, content_id = find_local(db, bundle.slug, load_asset=_load_asset_bytes)
    return plan_import(bundle, local, content_id=content_id, overwrite=overwrite)


@router.get("/{content_id}/bundle")
def export_content_bundle(content_id: UUID, db: DBSession, current_user: CurrentUser):
    """Export content as a git-native bundle: a directory of reviewable files.

    Unlike `?format=json`, nothing in it varies between two exports of
    unchanged content, so a diff shows what an author changed and nothing else.

    Deliberately stricter than the read rule: library roles and the owner
    only. A bundle is the authored form -- it exists to move a document
    between environments intact, walkthrough and all -- so a learner allowed
    to export one would hold the Knowledge Check answer key. Stripping the
    bundle instead would be worse than refusing it: the copy would import
    elsewhere as a quiz with no right answer, and nothing would say so.
    """
    from proving_ground.content.archive import pack
    from proving_ground.content.store import bundle_from_content

    content = db.query(Content).filter(Content.id == content_id).first()
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    if not may_read_authored_form(content, current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Exporting a bundle is for the content library",
        )

    bundle = bundle_from_content(content, load_asset=_load_asset_bytes)
    archive = pack(bundle.files(), bundle.slug)
    return Response(
        content=archive,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{bundle.slug}-bundle.tar.gz"'},
    )


@router.post("/bundle/preview", response_model=BundleDecision)
async def preview_content_bundle(
    db: DBSession,
    current_user: AuthorUser,
    file: UploadFile = File(...),
    # Annotated, not `= Query(...)`: with the older style the Python default is
    # the Query object itself, which is truthy, so any caller that is not
    # FastAPI gets overwrite=True.
    overwrite: Annotated[bool, Query(description="Plan as if replacing local changes")] = False,
):
    """Report what importing this bundle would do, without writing anything.

    It writes nothing, but the conflict report quotes the local document field
    by field, so it reads a draft to whoever may call it -- hence AuthorUser.
    """
    bundle = await _read_bundle_upload(file)
    return _decision_response(_plan(db, bundle, overwrite))


@router.post("/bundle/import", response_model=BundleImportResult)
async def import_content_bundle(
    db: DBSession,
    current_user: AuthorUser,
    file: UploadFile = File(...),
    overwrite: Annotated[
        bool, Query(description="Replace content that exists and differs")
    ] = False,
):
    """Import a bundle. Idempotent: re-importing identical content writes nothing.

    Content that already exists and differs is reported as a conflict and left
    alone unless `overwrite` is set.
    """
    from proving_ground.content.store import apply_bundle

    bundle = await _read_bundle_upload(file)
    decision = _plan(db, bundle, overwrite)

    content = None
    if decision.writes:
        try:
            content = apply_bundle(
                db,
                bundle,
                decision,
                current_user.id,
                render_html=render_markdown_to_html,
                save_asset=_save_asset_bytes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        db.commit()
        logger.info(
            "Content bundle %s: %s by %s", decision.action, bundle.slug, current_user.username
        )

    return BundleImportResult(
        decision=_decision_response(decision),
        content_id=content.id if content else decision.content_id,
        imported=content is not None,
    )


# ============ Assets ============


@router.post("/{content_id}/assets", response_model=ContentAssetResponse)
async def upload_asset(
    content_id: UUID,
    file: UploadFile = File(...),
    db: DBSession = None,
    current_user: CurrentUser = None,
):
    """Upload an asset (image) to content."""
    content = db.query(Content).filter(Content.id == content_id).first()
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    _require_owner_or_admin(content, current_user)

    # Validate file type
    allowed_types = ["image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400, detail=f"File type not allowed. Allowed: {allowed_types}"
        )

    # Read file and calculate hash
    file_content = await file.read()
    file_hash = hashlib.sha256(file_content).hexdigest()
    file_size = len(file_content)

    # Store in MinIO
    from proving_ground.services.object_store import object_client

    minio_client = object_client()

    bucket_name = content_bucket()
    # Ensure bucket exists
    if not minio_client.bucket_exists(bucket_name):
        minio_client.make_bucket(bucket_name)

    file_path = f"{content_id}/{file_hash[:8]}_{file.filename}"

    from io import BytesIO

    minio_client.put_object(
        bucket_name,
        file_path,
        BytesIO(file_content),
        file_size,
        content_type=file.content_type,
    )

    # Create database record
    asset = ContentAsset(
        content_id=content_id,
        filename=file.filename,
        file_path=f"{bucket_name}/{file_path}",
        mime_type=file.content_type,
        file_size=file_size,
        sha256_hash=file_hash,
    )

    db.add(asset)
    db.commit()
    db.refresh(asset)

    logger.info(f"Asset uploaded: {file.filename} to content {content_id}")
    return asset


@router.delete("/{content_id}/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset(
    content_id: UUID,
    asset_id: UUID,
    db: DBSession,
    current_user: CurrentUser,
):
    """Delete an asset from content."""
    content = db.query(Content).filter(Content.id == content_id).first()
    if not content:
        raise HTTPException(status_code=404, detail="Content not found")

    _require_owner_or_admin(content, current_user)

    asset = (
        db.query(ContentAsset)
        .filter(
            ContentAsset.id == asset_id,
            ContentAsset.content_id == content_id,
        )
        .first()
    )
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Delete from MinIO
    try:
        from proving_ground.services.object_store import object_client

        minio_client = object_client()

        parts = asset.file_path.split("/", 1)
        if len(parts) == 2:
            bucket_name, object_name = parts
            minio_client.remove_object(bucket_name, object_name)
    except Exception as e:
        logger.warning(f"Failed to delete asset from MinIO: {e}")

    db.delete(asset)
    db.commit()

    logger.info(f"Asset deleted: {asset.filename} from content {content_id}")


@router.get("/assets/{asset_id}")
def serve_asset(asset_id: UUID, db: DBSession):
    """Serve a content asset's raw bytes.

    Referenced directly by <img> tags in rendered walkthrough content, which
    cannot carry an Authorization header — so this endpoint is intentionally
    unauthenticated. It only serves assets whose parent content is published.

    That is a weaker gate than may_read_content, which also withholds the
    instructor-facing types. Matching it here would blank out the images in an
    author's own view of their instructor notes, since the editor loads them
    through this same URL. Closing the gap needs a signed, short-lived asset
    URL rather than a tighter filter.
    """
    asset = db.query(ContentAsset).filter(ContentAsset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    content = db.query(Content).filter(Content.id == asset.content_id).first()
    if not content or not content.is_published:
        # Do not reveal draft assets; 404 rather than 403 to avoid leaking existence.
        raise HTTPException(status_code=404, detail="Asset not found")

    # file_path is stored as "<bucket>/<object>"
    parts = asset.file_path.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=500, detail="Malformed asset path")
    bucket_name, object_name = parts

    from minio.error import S3Error

    from proving_ground.services.object_store import object_client

    minio_client = object_client()
    try:
        obj = minio_client.get_object(bucket_name, object_name)
        data = obj.read()
        obj.close()
        obj.release_conn()
    except S3Error as exc:
        raise HTTPException(status_code=404, detail="Asset object not found") from exc

    return Response(
        content=data,
        media_type=asset.mime_type or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ============ Content Types ============


@router.get("/types/available")
def get_content_types():
    """Get available content types."""
    return [{"value": ct.value, "label": ct.value.replace("_", " ").title()} for ct in ContentType]


# ============ Student Guides ============


@router.get("/student-guides/available", response_model=List[ContentListResponse])
def list_available_student_guides(
    db: DBSession,
    current_user: CurrentUser,
):
    """
    List published student guides available for range assignment.

    Returns only content of type 'student_guide' that has been published.
    Used by the Training tab in Range settings.

    No further check: a published student guide is readable by every role
    under may_read_content, so this list can hold nothing the caller could not
    already fetch by id.
    """
    return (
        db.query(Content)
        .filter(Content.content_type == ContentType.STUDENT_GUIDE, Content.is_published == True)
        .order_by(Content.title)
        .all()
    )
