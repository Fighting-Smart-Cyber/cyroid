"""Moving content between the library and a bundle (PG-104).

The bundle format and the import decision are both pure; this is the part that
knows about SQLAlchemy rows and object storage. Asset bytes are reached through
injected callables rather than a direct MinIO dependency, so the round trip can
be tested without one.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from proving_ground.content.bundle import (
    BundleAsset,
    ContentBundle,
    sha256_of,
    slug_for,
)
from proving_ground.content.importer import CREATE, UPDATE, ImportDecision
from proving_ground.models.content import Content, ContentAsset, ContentType

logger = logging.getLogger(__name__)

# Returns the asset's bytes, or None when they cannot be fetched.
AssetLoader = Callable[[ContentAsset], Optional[bytes]]
# Stores bytes for a newly imported asset and returns its storage path.
AssetSaver = Callable[[UUID, str, str, bytes], str]


def bundle_from_content(
    content: Content,
    *,
    assets: Optional[List[ContentAsset]] = None,
    load_asset: Optional[AssetLoader] = None,
) -> ContentBundle:
    """Render a library row as a bundle.

    Asset bytes are included only when ``load_asset`` supplies them; without it
    the manifest still lists every asset with its recorded hash, so the bundle
    describes the content honestly even when object storage is unreachable.
    """
    rows = assets if assets is not None else list(content.assets or [])
    bundle_assets: List[BundleAsset] = []
    for row in rows:
        data = load_asset(row) if load_asset else None
        digest = row.sha256_hash or (sha256_of(data) if data else "")
        bundle_assets.append(
            BundleAsset(
                filename=row.filename,
                mime_type=row.mime_type or "application/octet-stream",
                size=row.file_size or (len(data) if data else 0),
                sha256=digest,
                data=data,
            )
        )

    content_type = content.content_type
    return ContentBundle(
        slug=slug_for(content.title),
        title=content.title,
        description=content.description,
        content_type=getattr(content_type, "value", content_type) or "custom",
        version=content.version or "1.0",
        organization=content.organization,
        tags=list(content.tags or []),
        body_markdown=content.body_markdown or "",
        walkthrough=content.walkthrough_data,
        assets=bundle_assets,
    )


def find_local(
    db: Session, slug: str, *, load_asset: Optional[AssetLoader] = None
) -> Tuple[Optional[ContentBundle], Optional[UUID]]:
    """Find the library's copy of the content a bundle identifies.

    Matching is by slug rather than id: the same content promoted from one
    environment to another is a different row with a different UUID, so an id
    would make every promotion look like a new item.
    """
    for content in db.query(Content).all():
        if slug_for(content.title) == slug:
            return bundle_from_content(content, load_asset=load_asset), content.id
    return None, None


def apply_bundle(
    db: Session,
    bundle: ContentBundle,
    decision: ImportDecision,
    user_id: UUID,
    *,
    render_html: Optional[Callable[[str], str]] = None,
    save_asset: Optional[AssetSaver] = None,
) -> Optional[Content]:
    """Carry out an import decision. Returns None when the decision writes nothing.

    Raises:
        ValueError: if asked to update content that is no longer there.
    """
    if not decision.writes:
        return None

    body_html = render_html(bundle.body_markdown) if render_html and bundle.body_markdown else None
    fields = {
        "title": bundle.title,
        "description": bundle.description,
        "content_type": _content_type(bundle.content_type),
        "body_markdown": bundle.body_markdown,
        "body_html": body_html,
        "walkthrough_data": bundle.walkthrough,
        "version": bundle.version,
        "tags": sorted(bundle.tags or []),
        "organization": bundle.organization,
    }

    if decision.action == CREATE:
        content = Content(created_by_id=user_id, **fields)
        db.add(content)
        db.flush()
    elif decision.action == UPDATE:
        content = db.query(Content).filter(Content.id == decision.content_id).first()
        if content is None:
            raise ValueError(
                f"content '{decision.slug}' was in the library when the import was "
                "planned and is not there now"
            )
        for key, value in fields.items():
            setattr(content, key, value)
        db.flush()
    else:  # pragma: no cover - guarded by decision.writes above
        raise ValueError(f"cannot apply a {decision.action} decision")

    _apply_assets(db, content, bundle, save_asset)
    return content


def _apply_assets(
    db: Session,
    content: Content,
    bundle: ContentBundle,
    save_asset: Optional[AssetSaver],
) -> None:
    """Make the row's assets match the bundle's.

    Assets carried without bytes are left alone rather than deleted: a bundle
    exported while object storage was unreachable lists them but cannot ship
    them, and dropping the local copies would turn that into data loss.
    """
    existing: Dict[str, ContentAsset] = {a.filename: a for a in (content.assets or [])}
    incoming = {a.filename: a for a in bundle.assets}

    for filename, asset in incoming.items():
        if asset.data is None:
            continue
        row = existing.get(filename)
        if row is not None and row.sha256_hash == asset.sha256:
            continue
        if save_asset is None:
            logger.warning(
                "asset '%s' for '%s' was not imported: no asset store configured",
                filename,
                bundle.slug,
            )
            continue
        path = save_asset(content.id, filename, asset.mime_type, asset.data)
        if row is None:
            db.add(
                ContentAsset(
                    content_id=content.id,
                    filename=filename,
                    file_path=path,
                    mime_type=asset.mime_type,
                    file_size=len(asset.data),
                    sha256_hash=asset.sha256,
                )
            )
        else:
            row.file_path = path
            row.mime_type = asset.mime_type
            row.file_size = len(asset.data)
            row.sha256_hash = asset.sha256
    db.flush()


def _content_type(value: str) -> ContentType:
    try:
        return ContentType(value)
    except ValueError:
        logger.warning("unknown content_type %r in bundle; storing as custom", value)
        return ContentType.CUSTOM
