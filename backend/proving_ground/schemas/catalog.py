# backend/proving_ground/schemas/catalog.py
"""Pydantic schemas for Catalog API."""

import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from proving_ground.models.catalog import CatalogItemType, CatalogSourceType, CatalogSyncStatus

# ============ Catalog Source Validation ============

# git reaches well beyond fetching a repository when it is handed the wrong
# argument. A URL of the form '<helper>::<address>' runs that remote helper --
# 'ext::sh -c ...' executes its argument outright -- and an argument beginning
# with a dash is read as an option, of which '--upload-pack=' and '--config='
# both name a program to run. A catalog URL is admin-supplied but it is not a
# shell, so only the transports the product clones over are accepted.
GIT_URL_SCHEMES = ("https", "http", "ssh", "git")
HTTP_URL_SCHEMES = ("https", "http")

_URL_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://")
# The scp-like SSH shorthand, 'git@host:org/repo.git', which git accepts and
# private catalogs commonly use. The character after the colon may not be
# another colon, so a remote-helper address cannot be smuggled through it.
_SCP_LIKE_RE = re.compile(r"^[A-Za-z0-9._\-]+@[A-Za-z0-9._\-]+:[^:]")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/+\-]+$")


def _reject_control_characters(value: str, what: str) -> None:
    """Refuse a value carrying characters that split or terminate an argument."""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(f"catalog source {what} must not contain control characters")


def validate_source_url(url: str, source_type: CatalogSourceType) -> str:
    """Return a catalog source URL that is safe to hand to git or httpx.

    Raises:
        ValueError: if the URL could be read as a git option, names a transport
            the product does not clone over, or carries characters that would
            break out of the argument.
    """
    text = (url or "").strip()
    if not text:
        raise ValueError("catalog source url must not be empty")
    _reject_control_characters(text, "url")
    if text.startswith("-"):
        raise ValueError(
            "catalog source url must not begin with '-': git would read it as an option"
        )

    if source_type == CatalogSourceType.LOCAL:
        # A local source is a filesystem path that is opened, never executed.
        return text

    if any(ch.isspace() for ch in text):
        raise ValueError("catalog source url must not contain whitespace")

    allowed = GIT_URL_SCHEMES if source_type == CatalogSourceType.GIT else HTTP_URL_SCHEMES
    scheme_match = _URL_SCHEME_RE.match(text)
    if scheme_match:
        scheme = scheme_match.group(1).lower()
        if scheme not in allowed:
            raise ValueError(
                f"catalog source url scheme '{scheme}' is not supported; "
                f"use one of {', '.join(allowed)}"
            )
        return text

    if source_type == CatalogSourceType.GIT:
        if _SCP_LIKE_RE.match(text):
            return text
        if text.startswith("/"):
            # An air-gapped site clones from a mirror on disk. An absolute path
            # is neither an option nor a remote helper, so it stays allowed.
            return text

    raise ValueError(
        f"catalog source url must begin with one of {', '.join(f'{s}://' for s in allowed)}"
    )


def validate_source_url_of_unknown_type(url: str) -> str:
    """Validate a URL on an update, where the payload does not say the source type.

    An absolute path is taken to be a local source; anything else is held to the
    git rules, which are the strictest of the three. The type-aware check at sync
    time is still the authoritative one.
    """
    text = (url or "").strip()
    if text.startswith("/"):
        return validate_source_url(text, CatalogSourceType.LOCAL)
    return validate_source_url(text, CatalogSourceType.GIT)


def validate_source_branch(branch: Optional[str]) -> str:
    """Return a branch name that git cannot read as an option.

    An empty branch is allowed and means the remote's default branch.
    """
    text = (branch or "").strip()
    if not text:
        return ""
    _reject_control_characters(text, "branch")
    if text.startswith("-"):
        raise ValueError(
            "catalog source branch must not begin with '-': git would read it as an option"
        )
    if text.startswith("/") or text.endswith("/") or ".." in text:
        raise ValueError(f"catalog source branch is not a valid git ref: {branch!r}")
    if not _BRANCH_RE.match(text):
        raise ValueError(
            "catalog source branch may contain only letters, digits and the characters . _ / + -"
        )
    return text


# ============ Catalog Item Validation ============


def validate_item_path(path: str) -> str:
    """Refuse a declared item path that does not stay inside the catalog.

    index.json is fetched from a remote repository, so every path in it is
    attacker-controlled. This rejects the shape; the service resolves the path
    against the catalog root and rejects the result too, because a symlink
    committed into the catalog escapes without a '..' anywhere in the index.
    """
    text = (path or "").strip()
    if not text:
        return ""
    _reject_control_characters(text, "item path")
    if "\\" in text:
        raise ValueError(f"catalog item path must use '/' separators: {path!r}")
    posix = PurePosixPath(text)
    if posix.is_absolute() or ".." in posix.parts:
        raise ValueError(f"catalog item path must stay inside the catalog: {path!r}")
    return text


def validate_item_id(item_id: str) -> str:
    """Refuse an item id that would become a path segment somewhere.

    The id names the scenario file written into the scenario directory and the
    project directory copied into the image library, so a separator or a '..' in
    it writes outside them.
    """
    if not item_id:
        return item_id
    _reject_control_characters(item_id, "item id")
    if "/" in item_id or "\\" in item_id or item_id.strip() in (".", ".."):
        raise ValueError(f"catalog item id must not be a path: {item_id!r}")
    return item_id


# ============ Catalog Source Schemas ============


class CatalogSourceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    source_type: CatalogSourceType = CatalogSourceType.GIT
    url: str = Field(..., min_length=1, max_length=500)
    branch: str = Field(default="main", max_length=100)
    enabled: bool = True

    @model_validator(mode="after")
    def _check_url_and_branch(self) -> "CatalogSourceCreate":
        self.url = validate_source_url(self.url, self.source_type)
        self.branch = validate_source_branch(self.branch)
        return self


class CatalogSourceUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    url: Optional[str] = Field(None, min_length=1, max_length=500)
    branch: Optional[str] = Field(None, max_length=100)
    enabled: Optional[bool] = None

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_source_url_of_unknown_type(value)

    @field_validator("branch")
    @classmethod
    def _check_branch(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_source_branch(value)


class CatalogSourceResponse(BaseModel):
    id: UUID
    name: str
    source_type: CatalogSourceType
    url: str
    branch: str
    enabled: bool
    sync_status: CatalogSyncStatus
    error_message: Optional[str] = None
    item_count: int = 0
    last_synced: Optional[datetime] = None
    created_by: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ============ Catalog Item Schemas (from index.json) ============


class CatalogItemSummary(BaseModel):
    """An item from the catalog index."""

    id: str
    type: CatalogItemType
    name: str
    description: str = ""
    tags: List[str] = []
    version: str = "1.0"
    path: str = ""
    checksum: str = ""
    # Blueprint-specific
    requires_images: List[str] = []
    requires_base_images: List[str] = []
    includes_msel: bool = False
    includes_content: bool = False
    # Image-specific
    arch: Optional[str] = None
    docker_tag: Optional[str] = None
    # Install status (populated at query time)
    installed: bool = False
    installed_version: Optional[str] = None
    update_available: bool = False

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return validate_item_id(value)

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return validate_item_path(value)


class CatalogItemDetail(CatalogItemSummary):
    """Full item detail including README content."""

    readme: Optional[str] = None
    source_id: Optional[UUID] = None


# ============ Installed Item Schemas ============


class CatalogInstalledItemResponse(BaseModel):
    id: UUID
    catalog_source_id: UUID
    catalog_item_id: str
    item_type: CatalogItemType
    item_name: str
    installed_version: str
    installed_checksum: Optional[str] = None
    local_resource_id: Optional[UUID] = None
    installed_by: Optional[UUID] = None
    installed_at: datetime
    update_available: bool = False

    class Config:
        from_attributes = True


# ============ Install Request ============


class CatalogInstallRequest(BaseModel):
    source_id: UUID
    build_images: bool = True


# ============ Catalog Index (from index.json) ============


class CatalogIndex(BaseModel):
    catalog: dict = {}
    items: List[CatalogItemSummary] = []
