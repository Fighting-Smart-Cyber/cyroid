"""Deciding what importing a content bundle would do (PG-104).

The existing `/content/import` always creates a new row. Import the same file
twice and the library has two copies; import a corrected version and it has the
old one as well. For content promoted between environments by GitOps that is
the wrong shape entirely -- promotion runs the same import repeatedly and must
converge, not accumulate.

So importing is decided before it is done. Comparing the incoming bundle with
what is already in the library yields one of four answers, and only two of them
write anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional
from uuid import UUID

from proving_ground.content.bundle import ContentBundle

CREATE = "create"
UPDATE = "update"
UNCHANGED = "unchanged"
CONFLICT = "conflict"

# Fields compared field by field. Body, walkthrough and assets are compared too
# but reported as a marker rather than inline, because a conflict report that
# embeds two copies of a student guide is not a report anybody reads.
SCALAR_FIELDS = ("title", "description", "content_type", "version", "organization")


@dataclass
class FieldConflict:
    """One field where the library and the bundle disagree."""

    field: str
    local: Any = None
    incoming: Any = None
    summary: str = ""

    def describe(self) -> str:
        if self.summary:
            return f"{self.field}: {self.summary}"
        return f"{self.field}: {self.local!r} -> {self.incoming!r}"


@dataclass
class ImportDecision:
    """What importing one bundle would do, and why."""

    slug: str
    title: str
    action: str
    content_id: Optional[UUID] = None
    conflicts: List[FieldConflict] = field(default_factory=list)
    reason: str = ""

    @property
    def writes(self) -> bool:
        return self.action in (CREATE, UPDATE)


def diff_bundles(local: ContentBundle, incoming: ContentBundle) -> List[FieldConflict]:
    """Every difference between what is in the library and what is being imported."""
    conflicts: List[FieldConflict] = []

    for name in SCALAR_FIELDS:
        mine, theirs = getattr(local, name), getattr(incoming, name)
        if mine != theirs:
            conflicts.append(FieldConflict(field=name, local=mine, incoming=theirs))

    mine_tags, their_tags = sorted(local.tags or []), sorted(incoming.tags or [])
    if mine_tags != their_tags:
        conflicts.append(FieldConflict(field="tags", local=mine_tags, incoming=their_tags))

    if local.body_markdown != incoming.body_markdown:
        conflicts.append(
            FieldConflict(
                field="body_markdown",
                summary=_lines_summary(local.body_markdown, incoming.body_markdown),
            )
        )

    if local.walkthrough != incoming.walkthrough:
        conflicts.append(
            FieldConflict(field="walkthrough", summary="structured walkthrough differs")
        )

    conflicts.extend(_asset_conflicts(local, incoming))
    return conflicts


def _lines_summary(local: str, incoming: str) -> str:
    mine = len((local or "").splitlines())
    theirs = len((incoming or "").splitlines())
    if mine == theirs:
        return f"differs ({mine} lines either side)"
    return f"differs ({mine} lines -> {theirs} lines)"


def _asset_conflicts(local: ContentBundle, incoming: ContentBundle) -> List[FieldConflict]:
    mine = {a.filename: a.sha256 for a in local.assets}
    theirs = {a.filename: a.sha256 for a in incoming.assets}
    out: List[FieldConflict] = []

    for name in sorted(set(mine) | set(theirs)):
        if name not in theirs:
            out.append(
                FieldConflict(field=f"assets/{name}", summary="present locally, absent in bundle")
            )
        elif name not in mine:
            out.append(FieldConflict(field=f"assets/{name}", summary="new in bundle"))
        elif mine[name] != theirs[name]:
            out.append(FieldConflict(field=f"assets/{name}", summary="contents differ"))
    return out


def plan_import(
    incoming: ContentBundle,
    local: Optional[ContentBundle] = None,
    *,
    content_id: Optional[UUID] = None,
    overwrite: bool = False,
) -> ImportDecision:
    """Decide what importing ``incoming`` would do.

    Args:
        incoming: the bundle being imported.
        local: the same content as it exists in the library, if it does.
        content_id: the local row's id, carried through to the decision.
        overwrite: whether the caller has accepted replacing local changes.

    Returns:
        A decision. Without ``overwrite``, content that exists and differs is
        reported as a conflict and nothing is written -- silently replacing an
        author's local edits is the one outcome that cannot be undone.
    """
    if local is None:
        return ImportDecision(
            slug=incoming.slug,
            title=incoming.title,
            action=CREATE,
            reason="not in the library yet",
        )

    if local.fingerprint() == incoming.fingerprint():
        return ImportDecision(
            slug=incoming.slug,
            title=incoming.title,
            action=UNCHANGED,
            content_id=content_id,
            reason="already identical",
        )

    conflicts = diff_bundles(local, incoming)
    if overwrite:
        return ImportDecision(
            slug=incoming.slug,
            title=incoming.title,
            action=UPDATE,
            content_id=content_id,
            conflicts=conflicts,
            reason=f"overwriting {len(conflicts)} differing field(s)",
        )

    return ImportDecision(
        slug=incoming.slug,
        title=incoming.title,
        action=CONFLICT,
        content_id=content_id,
        conflicts=conflicts,
        reason=(
            f"already in the library and differs in {len(conflicts)} field(s); "
            "re-run with overwrite to replace it"
        ),
    )
