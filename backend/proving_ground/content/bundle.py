"""A git-native bundle for content (PG-104).

Content is authored by engineers, promoted between environments by GitOps,
and has to ship air-gapped. All three want the same thing: the portable
artifact should be a directory of ordinary files that git can diff and a person
can review.

The existing `/content/{id}/export?format=json` is not that. It returns one JSON
object with the markdown embedded as a quoted string, so every newline is an
escape and a one-word change shows as a single rewritten line. It also stamps
`exported_at` into the payload, which means exporting the same unchanged content
twice produces two different files -- git sees a change where none happened, and
review becomes meaningless. It drops walkthrough data and assets entirely.

A bundle is a directory instead:

    <slug>/
      content.yaml       metadata, fixed key order, no timestamps
      body.md            the markdown, as markdown
      walkthrough.yaml   the structured walkthrough, when there is one
      assets.yaml        manifest: filename, mime type, size, sha256
      assets/<filename>  the asset bytes, verbatim

Nothing in it varies between two exports of the same content, which is the
whole point: a diff shows what an author changed and nothing else.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import yaml

SCHEMA_VERSION = 1

CONTENT_FILE = "content.yaml"
BODY_FILE = "body.md"
WALKTHROUGH_FILE = "walkthrough.yaml"
ASSET_MANIFEST = "assets.yaml"
ASSET_DIR = "assets"

# Key order in content.yaml. Explicit rather than alphabetical so the file
# reads top-down like a document header, and fixed so a diff never reorders.
METADATA_KEYS: Sequence[str] = (
    "schema_version",
    "id",
    "title",
    "description",
    "content_type",
    "version",
    "organization",
    "tags",
)

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class BundleError(ValueError):
    """The bundle is not one we can read."""


def slug_for(title: str) -> str:
    """A stable, filesystem- and git-friendly identity derived from the title.

    Identity has to survive crossing environments, and a database UUID does
    not: the same content promoted from dev to prod is a different row with a
    different id. The title is what an author actually keeps stable, so the
    slug is derived from it.
    """
    slug = _SLUG_STRIP.sub("-", (title or "").strip().lower()).strip("-")
    return slug or "untitled"


@dataclass
class BundleAsset:
    filename: str
    mime_type: str
    size: int
    sha256: str
    data: Optional[bytes] = None

    def manifest_entry(self) -> Dict[str, Any]:
        return {
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass
class ContentBundle:
    """One content item, in the shape it takes on disk."""

    slug: str
    title: str
    description: Optional[str] = None
    content_type: str = "custom"
    version: str = "1.0"
    organization: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    body_markdown: str = ""
    walkthrough: Optional[dict] = None
    assets: List[BundleAsset] = field(default_factory=list)

    def metadata(self) -> Dict[str, Any]:
        values = {
            "schema_version": SCHEMA_VERSION,
            "id": self.slug,
            "title": self.title,
            "description": self.description,
            "content_type": self.content_type,
            "version": self.version,
            "organization": self.organization,
            # Tags come out of a JSON column in no particular order; sorting
            # them stops a reordering from reading as a change.
            "tags": sorted(self.tags or []),
        }
        return {key: values[key] for key in METADATA_KEYS}

    def files(self) -> Dict[str, bytes]:
        """The whole bundle as path -> bytes, relative to the bundle root."""
        out: Dict[str, bytes] = {
            CONTENT_FILE: _dump_yaml(self.metadata()),
            BODY_FILE: _text(self.body_markdown),
        }
        if self.walkthrough:
            out[WALKTHROUGH_FILE] = _dump_yaml(self.walkthrough, sort_keys=True)
        if self.assets:
            ordered = sorted(self.assets, key=lambda a: a.filename)
            out[ASSET_MANIFEST] = _dump_yaml({"assets": [a.manifest_entry() for a in ordered]})
            for asset in ordered:
                if asset.data is not None:
                    out[f"{ASSET_DIR}/{asset.filename}"] = asset.data
        return out

    def fingerprint(self) -> str:
        """A digest of everything an author could have changed.

        Used to decide whether an import is a no-op. It covers the asset
        bytes through their recorded hashes, so a changed image is a changed
        bundle even though the manifest entry is the only text that moves.
        """
        digest = hashlib.sha256()
        for path, payload in sorted(self.files().items()):
            digest.update(path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(payload)
            digest.update(b"\0")
        return digest.hexdigest()


def _dump_yaml(data: Any, *, sort_keys: bool = False) -> bytes:
    return yaml.safe_dump(
        data,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=sort_keys,
        width=100,
    ).encode("utf-8")


def _text(value: Optional[str]) -> bytes:
    """Normalise line endings and guarantee a trailing newline.

    Without this a bundle written on Windows and one written on Linux differ
    everywhere, and a missing final newline makes every subsequent diff start
    with a spurious hunk.
    """
    body = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    if body and not body.endswith("\n"):
        body += "\n"
    return body.encode("utf-8")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bundle(files: Dict[str, bytes]) -> ContentBundle:
    """Parse a bundle back from its files.

    Raises:
        BundleError: if content.yaml is missing, unparseable, or written by a
            newer schema than this version understands.
    """
    raw = files.get(CONTENT_FILE)
    if raw is None:
        raise BundleError(f"bundle is missing {CONTENT_FILE}")

    try:
        meta = yaml.safe_load(raw.decode("utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise BundleError(f"{CONTENT_FILE} is not readable YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise BundleError(f"{CONTENT_FILE} is not a mapping")

    schema_version = meta.get("schema_version", SCHEMA_VERSION)
    if not isinstance(schema_version, int) or schema_version > SCHEMA_VERSION:
        raise BundleError(
            f"bundle uses schema version {schema_version}; this PROVING GROUND "
            f"understands up to {SCHEMA_VERSION}"
        )

    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        raise BundleError(f"{CONTENT_FILE} has no title")

    walkthrough = None
    if WALKTHROUGH_FILE in files:
        try:
            walkthrough = yaml.safe_load(files[WALKTHROUGH_FILE].decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            raise BundleError(f"{WALKTHROUGH_FILE} is not readable YAML: {exc}") from exc

    return ContentBundle(
        slug=meta.get("id") or slug_for(title),
        title=title,
        description=meta.get("description"),
        content_type=meta.get("content_type") or "custom",
        version=str(meta.get("version") or "1.0"),
        organization=meta.get("organization"),
        tags=list(meta.get("tags") or []),
        body_markdown=files.get(BODY_FILE, b"").decode("utf-8"),
        walkthrough=walkthrough,
        assets=_read_assets(files),
    )


def _read_assets(files: Dict[str, bytes]) -> List[BundleAsset]:
    raw = files.get(ASSET_MANIFEST)
    if raw is None:
        return []
    try:
        manifest = yaml.safe_load(raw.decode("utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise BundleError(f"{ASSET_MANIFEST} is not readable YAML: {exc}") from exc

    assets: List[BundleAsset] = []
    for entry in manifest.get("assets") or []:
        if not isinstance(entry, dict) or not entry.get("filename"):
            raise BundleError(f"{ASSET_MANIFEST} has an entry without a filename")
        filename = entry["filename"]
        data = files.get(f"{ASSET_DIR}/{filename}")
        if data is not None:
            actual = sha256_of(data)
            declared = entry.get("sha256")
            # A manifest that disagrees with the bytes means the bundle was
            # edited by hand or truncated in transit. Importing it would put
            # content in the library that does not match what was reviewed.
            if declared and declared != actual:
                raise BundleError(
                    f"asset '{filename}' does not match its recorded sha256 "
                    f"(manifest {declared[:12]}…, file {actual[:12]}…)"
                )
        assets.append(
            BundleAsset(
                filename=filename,
                mime_type=entry.get("mime_type") or "application/octet-stream",
                size=int(entry.get("size") or (len(data) if data else 0)),
                sha256=entry.get("sha256") or (sha256_of(data) if data else ""),
                data=data,
            )
        )
    return assets
