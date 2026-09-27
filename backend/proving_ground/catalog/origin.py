"""Where a locally-installed resource came from in a catalog."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import UUID


@dataclass
class CatalogOrigin:
    """The catalog item a local blueprint was installed from, and its source file.

    ``item_path`` is relative to the catalog root, so it is what belongs in a
    patch header and what a maintainer would see in their clone.
    """

    source_id: UUID
    source_name: str
    source_url: str
    source_branch: Optional[str]
    item_id: str
    item_name: str
    installed_version: str
    item_path: str
    yaml_path: Path
    document: dict
    text: str
