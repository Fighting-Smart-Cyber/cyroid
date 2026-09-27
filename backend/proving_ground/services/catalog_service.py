# backend/proving_ground/services/catalog_service.py
"""
Catalog service for managing catalog sources, browsing items, and installing content.

Catalog sources are Git repos, HTTP endpoints, or local directories that contain
an index.json describing available blueprints, scenarios, images, and base images.
The service handles syncing sources, browsing their indexes, and installing items
into the local PROVING GROUND instance.
"""
import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional
from uuid import UUID

import yaml
from pydantic import ValidationError
from sqlalchemy.orm import Session

from .registry_service import get_registry_service, RegistryPushError

from proving_ground.capability.blueprint import (
    SCHEMA_VERSION_K8S,
    SCHEMA_VERSION_LEGACY,
    read_blueprint,
)
from proving_ground.catalog.dependencies import (
    ERA_A_ON_KUBERNETES,
    ERA_B_ON_DOCKER,
    SUBSTRATE_DOCKER,
    SUBSTRATE_KUBERNETES,
)
from proving_ground.catalog.origin import CatalogOrigin
from proving_ground.config import get_settings
from proving_ground.models.base_image import BaseImage, ImageType
from proving_ground.models.blueprint import RangeBlueprint
from proving_ground.models.catalog import (
    CatalogInstalledItem,
    CatalogItemType,
    CatalogSource,
    CatalogSourceType,
    CatalogSyncStatus,
)
from proving_ground.models.content import Content, ContentType
from proving_ground.schemas.catalog import (
    CatalogItemDetail,
    CatalogItemSummary,
    validate_source_branch,
    validate_source_url,
)
from proving_ground.services.walkthrough_parser import parse_markdown_to_walkthrough

logger = logging.getLogger(__name__)

# Directory where Dockerfile image projects live
IMAGES_DIR = "/data/images"


def resolve_declared_path(
    root: Path,
    declared: str,
    *,
    what: str,
    root_label: str = "the catalog root",
) -> Path:
    """Resolve a path a catalog declared against ``root``, refusing any escape.

    index.json is fetched from a remote repository, so every path it names is
    attacker-controlled: a '../..' segment would otherwise let a browse read a
    file from anywhere the API can reach, and an install copy an arbitrary
    directory tree into /data. Symlinks are resolved before the containment
    check, because a symlink committed into the catalog escapes without a '..'
    appearing in the index at all. The same helper guards the directories an
    install writes into, where the declared segment is an item id.

    Raises:
        ValueError: if the resolved path lies outside ``root``.
    """
    text = str(declared or "").strip()
    if "\x00" in text:
        raise ValueError(f"{what} contains a null byte: {declared!r}")

    resolved_root = root.resolve()
    candidate = (resolved_root / text).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ValueError(f"{what} resolves outside {root_label}: {declared!r}")
    return candidate


def declared_schema_version(document: Optional[dict]) -> object:
    """The ``schemaVersion`` a blueprint document declares; absence means v1.

    Returned exactly as declared rather than coerced to an int. A version this engine does not
    understand is refused by ``read_blueprint``, which says so in a sentence; a silent fallback to
    1 here would install a newer blueprint as an Era A one and drop everything it declared.
    """
    if not isinstance(document, dict):
        return SCHEMA_VERSION_LEGACY
    return document.get("schemaVersion", SCHEMA_VERSION_LEGACY)


def catalog_item_schema_version(document: Optional[dict]) -> Optional[int]:
    """The version to report to the browser, or None when the document does not say.

    None is not the same answer as 1, which is why it is not folded into one. A blueprint that
    declares no ``schemaVersion`` is Era A by definition, but an *index entry* that does not carry
    the field may simply be from a catalog written before the field existed -- and the storefront
    words those two cases differently. A declared value that is not an integer is reported as
    unknown rather than guessed at; the install itself refuses it by name.
    """
    if not isinstance(document, dict) or "schemaVersion" not in document:
        return None
    declared = document["schemaVersion"]
    if isinstance(declared, bool) or not isinstance(declared, int):
        return None
    return declared


def build_config_from_yaml(blueprint_data: dict, *, item_name: str = "") -> dict:
    """Build the config JSON a RangeBlueprint stores, from a catalog blueprint document.

    One catalog serves both substrates, so this has to produce both shapes. An Era A document is
    converted field by field, because the catalog YAML and the stored config genuinely disagree
    about shape (``template_name`` versus ``base_image_tag``, events versus MSEL). An Era B
    document is already the shape ``capability.blueprint.read_blueprint`` reads, so converting it
    could only lose something -- which is exactly what used to happen: the result was assembled
    from a fixed set of Era A keys, and a v2 item's workloads and capabilities were dropped on the
    way in and never missed until the range refused to deploy.

    Raises:
        ValueError: if the document is not a blueprint this engine can read, named so that a
            catalog item is refused rather than stored half-read.
    """
    where = f"catalog item '{item_name}'" if item_name else "this catalog blueprint"
    # blueprint.yaml is remote content and may be any YAML document at all. A list or a bare
    # string reaches .get() below and raises AttributeError, which the install route turns into a
    # 500 rather than a refusal naming the item.
    if not isinstance(blueprint_data, dict):
        raise ValueError(f"{where} has a blueprint.yaml that is not a mapping")

    version = declared_schema_version(blueprint_data)
    if version == SCHEMA_VERSION_LEGACY:
        config = _legacy_config_from_yaml(blueprint_data)
    else:
        config = _kubernetes_config_from_yaml(blueprint_data, version)

    # Capabilities are era-neutral -- a capability package does not care which substrate runs it,
    # and read_blueprint reads them from a v1 config for that reason -- so they are carried
    # through whichever era wrote the document.
    if blueprint_data.get("capabilities") is not None:
        config["capabilities"] = blueprint_data["capabilities"]

    try:
        read_blueprint(config)
    except ValueError as exc:
        raise ValueError(f"{where} is not a blueprint this install can read: {exc}") from exc
    return config


def _kubernetes_config_from_yaml(blueprint_data: dict, version: object) -> dict:
    """Carry an Era B document through intact.

    Copied rather than translated, and the declared version is copied too rather than replaced
    with ``SCHEMA_VERSION_K8S``: a document declaring a version this engine does not know must
    reach ``read_blueprint`` still saying so, or it would be refused for the wrong reason.
    """
    config: dict = {"schemaVersion": version}
    for key in ("networks", "workloads", "content_ids"):
        if blueprint_data.get(key) is not None:
            config[key] = blueprint_data[key]
    return config


def _legacy_config_from_yaml(blueprint_data: dict) -> dict:
    """Build the Era A config: networks, VMs, a router and an MSEL."""
    config = {"networks": [], "vms": [], "router": blueprint_data.get("router"), "msel": None}

    # Convert networks
    for net in blueprint_data.get("networks", []):
        config["networks"].append(
            {
                "name": net.get("name"),
                "subnet": net.get("subnet"),
                "gateway": net.get("gateway"),
                "is_isolated": net.get("is_isolated", False),
                "internet_enabled": net.get("internet_enabled", False),
                "dhcp_enabled": net.get("dhcp_enabled", False),
            }
        )

    # Convert VMs
    for vm in blueprint_data.get("vms", []):
        vm_config = {
            "hostname": vm.get("hostname"),
            "cpu": vm.get("cpu", 1),
            "ram_mb": vm.get("ram_mb", 1024),
            "disk_gb": vm.get("disk_gb", 20),
            "position_x": vm.get("position_x"),
            "position_y": vm.get("position_y"),
        }

        # Handle network interfaces - prefer multi-NIC format, fall back to legacy
        if vm.get("network_interfaces"):
            # Multi-NIC format: list of network interface objects
            vm_config["network_interfaces"] = [
                {
                    "network_name": iface.get("network_name"),
                    "ip_address": iface.get("ip_address"),
                    "is_primary": iface.get("is_primary", False),
                }
                for iface in vm.get("network_interfaces", [])
            ]
        else:
            # Legacy single-NIC format
            vm_config["ip_address"] = vm.get("ip_address")
            vm_config["network_name"] = vm.get("network_name")

        # Prefer base_image_tag (new format), fall back to template_name (deprecated)
        if vm.get("base_image_tag"):
            vm_config["base_image_tag"] = vm.get("base_image_tag")
        elif vm.get("template_name"):
            vm_config["base_image_tag"] = vm.get("template_name")

        # Pass through Windows version for dockurr VMs
        if vm.get("windows_version"):
            vm_config["windows_version"] = vm.get("windows_version")

        # Pass through architecture override (for cross-platform pulls)
        if vm.get("arch"):
            vm_config["arch"] = vm.get("arch")

        # Pass through environment variables (used by dockurr VMs, service configs, etc.)
        if vm.get("environment"):
            vm_config["environment"] = vm.get("environment")

        config["vms"].append(vm_config)

    # Convert events to MSEL format if present
    events = blueprint_data.get("events", [])
    walkthrough = blueprint_data.get("walkthrough")
    if events or walkthrough:
        msel_content = yaml.dump({"events": events}, default_flow_style=False) if events else ""
        config["msel"] = {"content": msel_content, "format": "yaml", "walkthrough": walkthrough}

    return config


class CatalogService:
    """Service for catalog source management, index browsing, and content installation."""

    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()

    # =========================================================================
    # Source Management
    # =========================================================================

    def get_source_dir(self, source: CatalogSource) -> Path:
        """Get the local storage directory for a catalog source.

        Args:
            source: The catalog source model.

        Returns:
            Path to the local directory where this source is stored.
        """
        storage_root = Path(self.settings.catalog_storage_dir)
        storage_root.mkdir(parents=True, exist_ok=True)
        return storage_root / str(source.id)

    def sync_source(self, source: CatalogSource) -> int:
        """Sync a catalog source by cloning/pulling git, fetching HTTP, or verifying local.

        Updates the source's sync_status and item_count in the database.

        Args:
            source: The catalog source to sync.

        Returns:
            Number of items found in the index.

        Raises:
            Exception: If sync fails (also sets source status to ERROR).
        """
        source.sync_status = CatalogSyncStatus.SYNCING
        source.error_message = None
        self.db.commit()

        try:
            if source.source_type == CatalogSourceType.GIT:
                self._sync_git(source)
            elif source.source_type == CatalogSourceType.HTTP:
                self._sync_http(source)
            elif source.source_type == CatalogSourceType.LOCAL:
                self._sync_local(source)
            else:
                raise ValueError(f"Unknown source type: {source.source_type}")

            # Count items from the index
            index = self._load_index(source)
            item_count = len(index.get("items", []))

            source.sync_status = CatalogSyncStatus.IDLE
            source.item_count = item_count
            source.error_message = None
            self.db.commit()

            logger.info(f"Synced catalog source '{source.name}': {item_count} items")
            return item_count

        except Exception as e:
            source.sync_status = CatalogSyncStatus.ERROR
            source.error_message = str(e)[:500]
            self.db.commit()
            logger.error(f"Failed to sync catalog source '{source.name}': {e}")
            raise

    def _sync_git(self, source: CatalogSource) -> None:
        """Clone or pull a git catalog source.

        If the local directory already exists with a .git folder, does a pull.
        Otherwise, clones the repository fresh.

        Args:
            source: The git catalog source.

        Raises:
            ValueError: if the stored url or branch could be read by git as an
                option or names a transport that runs a command.
        """
        # Re-validated here rather than trusted from the schema: a row can reach
        # this point from an older release, from a restored database, or from a
        # caller that never went through CatalogSourceCreate.
        url = validate_source_url(source.url, CatalogSourceType.GIT)
        branch = validate_source_branch(source.branch)

        source_dir = self.get_source_dir(source)
        git_dir = source_dir / ".git"

        if git_dir.exists():
            # Pull existing repo
            logger.info(f"Pulling catalog source '{source.name}' from {source.url}")
            result = subprocess.run(
                ["git", "-C", str(source_dir), "pull", "--ff-only"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"git pull failed: {result.stderr.strip() or result.stdout.strip()}"
                )
        else:
            # Clone fresh
            logger.info(f"Cloning catalog source '{source.name}' from {source.url}")
            source_dir.mkdir(parents=True, exist_ok=True)

            cmd = ["git", "clone", "--depth", "1"]
            if branch:
                cmd.extend(["--branch", branch])
            # '--' so that whatever the url turns out to be, git reads it as a
            # remote and not as an option.
            cmd.extend(["--", url, str(source_dir)])

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                # Clean up partial clone
                if source_dir.exists():
                    shutil.rmtree(source_dir, ignore_errors=True)
                raise RuntimeError(
                    f"git clone failed: {result.stderr.strip() or result.stdout.strip()}"
                )

    def _sync_http(self, source: CatalogSource) -> None:
        """Fetch index.json from an HTTP catalog source.

        Downloads the index.json from the source URL and stores it locally.

        Args:
            source: The HTTP catalog source.
        """
        import httpx

        validated = validate_source_url(source.url, CatalogSourceType.HTTP)

        source_dir = self.get_source_dir(source)
        source_dir.mkdir(parents=True, exist_ok=True)

        # Ensure URL points to index.json
        url = validated.rstrip("/")
        if not url.endswith("index.json"):
            url = f"{url}/index.json"

        logger.info(f"Fetching catalog index from {url}")
        response = httpx.get(url, timeout=30.0, follow_redirects=True)
        response.raise_for_status()

        # Write index.json locally
        index_path = source_dir / "index.json"
        index_path.write_text(response.text, encoding="utf-8")

        logger.info(f"Fetched catalog index from {url}")

    def _sync_local(self, source: CatalogSource) -> None:
        """Verify that a local catalog source path exists and has an index.json.

        Args:
            source: The local catalog source.

        Raises:
            FileNotFoundError: If the path or index.json doesn't exist.
            ValueError: If the stored url is not usable as a filesystem path.
        """
        local_path = Path(validate_source_url(source.url, CatalogSourceType.LOCAL))
        if not local_path.exists():
            raise FileNotFoundError(f"Local catalog path does not exist: {source.url}")
        if not (local_path / "index.json").exists():
            raise FileNotFoundError(f"No index.json found at local catalog path: {source.url}")
        logger.info(f"Verified local catalog source at {source.url}")

    def delete_source_data(self, source: CatalogSource) -> None:
        """Delete the local data for a catalog source (rm -rf the clone directory).

        Args:
            source: The catalog source whose local data should be removed.
        """
        source_dir = self.get_source_dir(source)
        if source_dir.exists():
            shutil.rmtree(source_dir, ignore_errors=True)
            logger.info(f"Deleted catalog source data at {source_dir}")

    # =========================================================================
    # Index & Browsing
    # =========================================================================

    def _get_catalog_root(self, source: CatalogSource) -> Path:
        """Get the root directory for a catalog source.

        For LOCAL sources, uses the url path directly.
        For GIT and HTTP sources, uses the local storage directory.

        Args:
            source: The catalog source.

        Returns:
            Path to the catalog root directory.
        """
        if source.source_type == CatalogSourceType.LOCAL:
            return Path(source.url)
        return self.get_source_dir(source)

    def _file_in(self, item_dir: Path, name: str) -> Path:
        """Resolve one of the well-known filenames inside a catalog item directory.

        The filename is ours; the file it lands on is the catalog's. A symlink
        committed as blueprint.yaml, msel.md or README.md would otherwise be
        read from wherever it points, which is how a repository nobody trusts
        reads /etc back to the browser.

        Raises:
            ValueError: if the name resolves outside ``item_dir``.
        """
        return resolve_declared_path(
            item_dir,
            name,
            what=f"the catalog's {name}",
            root_label="its catalog item directory",
        )

    def _file_in_project(self, project_dir: Path, name: str) -> Path:
        """Resolve a metadata filename inside an installed image project.

        The project was copied out of a catalog with its symlinks intact, so the
        same rule applies as inside the catalog itself.

        Raises:
            ValueError: if the name resolves outside ``project_dir``.
        """
        return resolve_declared_path(
            project_dir,
            name,
            what=f"the image project's {name}",
            root_label="the image project directory",
        )

    def _load_index(self, source: CatalogSource) -> dict:
        """Load and parse the index.json for a catalog source.

        Args:
            source: The catalog source.

        Returns:
            Parsed index dictionary with 'catalog' and 'items' keys.

        Raises:
            FileNotFoundError: If index.json does not exist.
            json.JSONDecodeError: If index.json is malformed.
        """
        catalog_root = self._get_catalog_root(source)
        index_path = catalog_root / "index.json"

        if not index_path.exists():
            raise FileNotFoundError(f"index.json not found at {index_path}")

        with open(index_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def list_items(
        self,
        source: CatalogSource,
        item_type: Optional[CatalogItemType] = None,
        search: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> List[CatalogItemSummary]:
        """Browse items from a catalog source with optional filters and install status.

        Args:
            source: The catalog source to browse.
            item_type: Optional filter by item type (blueprint, scenario, image, base_image).
            search: Optional text search against name and description.
            tags: Optional tag filter (items must have at least one matching tag).

        Returns:
            List of CatalogItemSummary objects with install status populated.
        """
        index = self._load_index(source)
        items = index.get("items", [])

        # Build a lookup of installed items for this source
        installed_lookup: Dict[str, CatalogInstalledItem] = {}
        installed_items = (
            self.db.query(CatalogInstalledItem)
            .filter(CatalogInstalledItem.catalog_source_id == source.id)
            .all()
        )
        for inst in installed_items:
            installed_lookup[inst.catalog_item_id] = inst

        results: List[CatalogItemSummary] = []
        search_lower = search.lower() if search else None

        for item_data in items:
            # Filter by type
            if item_type and item_data.get("type") != item_type.value:
                continue

            # Filter by search text
            if search_lower:
                name_match = search_lower in item_data.get("name", "").lower()
                desc_match = search_lower in item_data.get("description", "").lower()
                if not name_match and not desc_match:
                    continue

            # Filter by tags
            if tags:
                item_tags = item_data.get("tags", [])
                if not any(t in item_tags for t in tags):
                    continue

            # Build summary with install status
            item_id = item_data.get("id", "")
            installed = installed_lookup.get(item_id)

            try:
                summary = self._summarise(item_data, item_id, installed)
            except (ValidationError, ValueError) as exc:
                # An entry the catalog declared unusably -- an id that is a path,
                # a path leaving the catalog, an unknown type -- is omitted
                # rather than served. Browsing must still render the rest of the
                # catalog: one bad entry may not blank the page.
                logger.warning(
                    f"Skipping catalog item '{item_id}' from source '{source.name}': {exc}"
                )
                continue
            results.append(summary)

        return results

    def _summarise(
        self,
        item_data: dict,
        item_id: str,
        installed: Optional[CatalogInstalledItem],
    ) -> CatalogItemSummary:
        """Build one browse row from an index entry, applying the schema's checks."""
        # Normalize arch (catalog may provide string, list, or null)
        raw_arch = item_data.get("arch")
        if isinstance(raw_arch, list):
            arch = ", ".join(raw_arch)
        else:
            arch = raw_arch

        # Filter out empty-string tags from catalog data
        cleaned_tags = [t for t in item_data.get("tags", []) if t]

        # From the index only. The storefront marks an item written for the other substrate before
        # anyone installs it, and the index is where a listing can learn that without opening one
        # file per row. `CatalogItemDetail` re-reads the blueprint document, which is the
        # authoritative answer, for the page carrying the Install button.
        return CatalogItemSummary(
            id=item_id,
            schema_version=catalog_item_schema_version(item_data),
            type=CatalogItemType(item_data.get("type", "blueprint")),
            name=item_data.get("name", ""),
            description=item_data.get("description", ""),
            tags=cleaned_tags,
            version=item_data.get("version", "1.0"),
            path=item_data.get("path", ""),
            checksum=item_data.get("checksum", ""),
            requires_images=item_data.get("requires_images", []),
            requires_base_images=item_data.get("requires_base_images", []),
            includes_msel=item_data.get("includes_msel", False),
            includes_content=item_data.get("includes_content", False),
            arch=arch,
            docker_tag=item_data.get("docker_tag"),
            installed=installed is not None,
            installed_version=installed.installed_version if installed else None,
            update_available=(
                installed is not None
                and installed.installed_version != item_data.get("version", "1.0")
            ),
        )

    def get_item_detail(
        self,
        source: CatalogSource,
        item_id: str,
    ) -> Optional[CatalogItemDetail]:
        """Get full detail for a specific catalog item including README and install status.

        Args:
            source: The catalog source.
            item_id: The unique item identifier within the catalog.

        Returns:
            CatalogItemDetail with readme content and install status, or None if
            the item is not in the index, or is declared in a way that puts it
            outside the catalog -- an unusable item is treated as absent, which
            is what browsing already shows and what install then refuses.
        """
        index = self._load_index(source)
        items = index.get("items", [])

        item_data = None
        for item in items:
            if item.get("id") == item_id:
                item_data = item
                break

        if not item_data:
            return None

        # Check install status
        installed = (
            self.db.query(CatalogInstalledItem)
            .filter(
                CatalogInstalledItem.catalog_source_id == source.id,
                CatalogInstalledItem.catalog_item_id == item_id,
            )
            .first()
        )

        try:
            readme_content = self._read_item_readme(source, item_id, item_data)
            detail = CatalogItemDetail(
                id=item_data.get("id", ""),
                schema_version=self._item_schema_version(source, item_id, item_data),
                type=CatalogItemType(item_data.get("type", "blueprint")),
                name=item_data.get("name", ""),
                description=item_data.get("description", ""),
                tags=item_data.get("tags", []),
                version=item_data.get("version", "1.0"),
                path=item_data.get("path", ""),
                checksum=item_data.get("checksum", ""),
                requires_images=item_data.get("requires_images", []),
                requires_base_images=item_data.get("requires_base_images", []),
                includes_msel=item_data.get("includes_msel", False),
                includes_content=item_data.get("includes_content", False),
                arch=item_data.get("arch"),
                docker_tag=item_data.get("docker_tag"),
                installed=installed is not None,
                installed_version=installed.installed_version if installed else None,
                update_available=(
                    installed is not None
                    and installed.installed_version != item_data.get("version", "1.0")
                ),
                readme=readme_content,
                source_id=source.id,
            )
        except (ValidationError, ValueError) as exc:
            logger.warning(f"Ignoring catalog item '{item_id}' from source '{source.name}': {exc}")
            return None
        return detail

    def _item_schema_version(
        self,
        source: CatalogSource,
        item_id: str,
        item_data: dict,
    ) -> Optional[int]:
        """The era of one item, preferring the blueprint document over the index entry.

        The index is a summary a catalog maintainer writes by hand and can forget to update; the
        document is what the installer will actually read, and what the install refusal will be
        decided from. A document present and silent is a definite answer -- a blueprint with no
        ``schemaVersion`` is Era A by definition -- so it reports 1 rather than "not stated".

        A document that cannot be read falls back to the index rather than raising: an item whose
        blueprint.yaml is missing or unparseable still has a detail page to render, and the
        install is where it is refused by name.
        """
        declared = catalog_item_schema_version(item_data)
        if item_data.get("type") != CatalogItemType.BLUEPRINT.value:
            return declared
        try:
            item_root = resolve_declared_path(
                self._get_catalog_root(source),
                item_data.get("path", ""),
                what=f"catalog item '{item_id}' path",
            )
            if not item_root.is_dir():
                return declared
            yaml_path = self._file_in(item_root, "blueprint.yaml")
            if not yaml_path.exists():
                return declared
            document = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
            logger.warning(f"Could not read the schema version for '{item_id}': {exc}")
            return declared
        if not isinstance(document, dict):
            return declared
        version = catalog_item_schema_version(document)
        return SCHEMA_VERSION_LEGACY if version is None else version

    def _read_item_readme(
        self,
        source: CatalogSource,
        item_id: str,
        item_data: dict,
    ) -> Optional[str]:
        """Read an item's README, from inside the catalog only.

        Raises:
            ValueError: if the item's declared path leaves the catalog root.
        """
        item_path = item_data.get("path", "")
        if not item_path:
            return None

        readme_path = resolve_declared_path(
            self._get_catalog_root(source),
            item_path,
            what=f"catalog item '{item_id}' path",
        )
        # If path points to a file (e.g., scenario YAML), there is no README.
        if not readme_path.is_dir():
            return None

        readme_file = self._file_in(readme_path, "README.md")
        if not readme_file.exists():
            return None
        try:
            return readme_file.read_text(encoding="utf-8")
        except (IOError, UnicodeDecodeError) as e:
            logger.warning(f"Could not read README for {item_id}: {e}")
            return None

    # =========================================================================
    # Installation
    # =========================================================================

    def install_item(
        self,
        source: CatalogSource,
        item_id: str,
        user_id: UUID,
        build_images: bool = False,
        install_dependencies: bool = True,
    ) -> CatalogInstalledItem:
        """Install a catalog item into the local PROVING GROUND instance.

        Dispatches to the appropriate installer based on item type.

        Args:
            source: The catalog source containing the item.
            item_id: The item identifier to install.
            user_id: The user performing the installation.
            build_images: Whether to build Docker images (for blueprints/images).

        Returns:
            CatalogInstalledItem record for the newly installed item.

        Raises:
            ValueError: If item is not found or already installed.
        """
        # Check if already installed
        existing = (
            self.db.query(CatalogInstalledItem)
            .filter(
                CatalogInstalledItem.catalog_source_id == source.id,
                CatalogInstalledItem.catalog_item_id == item_id,
            )
            .first()
        )
        if existing:
            raise ValueError(f"Item '{item_id}' is already installed from this source")

        # Load item detail from index
        detail = self.get_item_detail(source, item_id)
        if not detail:
            raise ValueError(f"Item '{item_id}' not found in catalog source")

        catalog_root = self._get_catalog_root(source)
        item_path = resolve_declared_path(
            catalog_root, detail.path, what=f"catalog item '{item_id}' path"
        )

        # Dispatch by type
        local_resource_id: Optional[UUID] = None

        if detail.type == CatalogItemType.BLUEPRINT:
            local_resource_id = self._install_blueprint(
                item_path,
                detail,
                user_id,
                build_images,
                catalog_root,
                install_dependencies=install_dependencies,
            )
        elif detail.type == CatalogItemType.SCENARIO:
            self._install_scenario(item_path, detail)
        elif detail.type == CatalogItemType.IMAGE:
            self._install_image(item_path, detail, build_images)
        elif detail.type == CatalogItemType.BASE_IMAGE:
            local_resource_id = self._install_base_image(item_path, detail)
        elif detail.type == CatalogItemType.CONTENT:
            local_resource_id = self._install_content(item_path, detail, user_id)
        else:
            raise ValueError(f"Unsupported item type: {detail.type}")

        # Compute checksum for the installed item
        checksum = detail.checksum or ""

        # Create installed item record
        installed_item = CatalogInstalledItem(
            catalog_source_id=source.id,
            catalog_item_id=item_id,
            item_type=detail.type,
            item_name=detail.name,
            installed_version=detail.version,
            installed_checksum=checksum,
            local_resource_id=local_resource_id,
            installed_by=user_id,
        )
        self.db.add(installed_item)
        self.db.commit()
        self.db.refresh(installed_item)

        logger.info(
            f"Installed catalog item '{detail.name}' (type={detail.type.value}, "
            f"id={item_id}) from source '{source.name}'"
        )
        return installed_item

    def _install_blueprint(
        self,
        item_path: Path,
        detail: CatalogItemDetail,
        user_id: UUID,
        build_images: bool,
        catalog_root: Path,
        install_dependencies: bool = True,
    ) -> UUID:
        """Install a blueprint item from the catalog.

        Reads blueprint.yaml, installs required images from the catalog,
        creates Content from walkthrough data, builds MSEL from events,
        and creates a RangeBlueprint.

        Args:
            item_path: Path to the blueprint directory.
            detail: The catalog item detail.
            user_id: User performing the install.
            build_images: Whether to build Docker images for required images.
            catalog_root: Root path of the catalog source.
            install_dependencies: Install referenced base images and Dockerfile
                projects. False when a caller has already installed them.

        Returns:
            UUID of the created RangeBlueprint.

        Raises:
            ValueError: if the catalog declares a path outside itself, for the
                blueprint or for anything the blueprint requires.
        """
        # Read blueprint.yaml. Resolved rather than joined because the file
        # inside the item directory may be a symlink out of the catalog.
        blueprint_yaml_path = self._file_in(item_path, "blueprint.yaml")
        if not blueprint_yaml_path.exists():
            raise FileNotFoundError(f"blueprint.yaml not found in {item_path}")

        with open(blueprint_yaml_path, "r", encoding="utf-8") as f:
            blueprint_data = yaml.safe_load(f)

        if not blueprint_data:
            raise ValueError("Empty blueprint.yaml")

        # Before anything is written, and before the dependency loop below copies image projects
        # onto disk for a blueprint this install could never deploy.
        self._refuse_blueprint_from_the_other_era(blueprint_data, detail)

        # Base images and Dockerfile projects are installed here only when
        # nobody upstream has taken responsibility for them. The one-click
        # installer (PG-149) resolves and installs them itself so each one is
        # a named, reportable step, and passes install_dependencies=False.
        if install_dependencies:
            # Install required images from catalog
            requires_images = detail.requires_images or []
            for image_name in requires_images:
                # The name is index data like any other path segment, so it is
                # resolved rather than joined.
                image_src_dir = resolve_declared_path(
                    catalog_root,
                    f"images/{image_name}",
                    what=f"required image '{image_name}'",
                )
                if image_src_dir.exists() and (image_src_dir / "Dockerfile").exists():
                    self._install_image_from_path(image_src_dir, image_name, build_images)
                else:
                    logger.warning(
                        f"Required image '{image_name}' not found in catalog at " f"{image_src_dir}"
                    )

            # Install required base images from catalog
            requires_base_images = detail.requires_base_images or []
            if requires_base_images:
                # Load the catalog index.json to find base_image items
                index_path = catalog_root / "index.json"
                base_image_items: Dict[str, dict] = {}
                if index_path.exists():
                    with open(index_path, "r", encoding="utf-8") as idx_f:
                        index_data = json.load(idx_f)
                    base_image_items = {
                        item.get("id"): item
                        for item in (index_data.get("items") or [])
                        if item.get("type") == "base_image"
                    }
                for bi_name in requires_base_images:
                    bi_item = base_image_items.get(bi_name)
                    if bi_item:
                        # Outside the try below: a path leaving the catalog is a
                        # hostile index, not a base image that failed to install.
                        bi_path = resolve_declared_path(
                            catalog_root,
                            bi_item.get("path", ""),
                            what=f"base image '{bi_name}' path",
                        )
                        try:
                            bi_detail = CatalogItemDetail(
                                id=bi_item.get("id", ""),
                                type=CatalogItemType.BASE_IMAGE,
                                name=bi_item.get("name", ""),
                                description=bi_item.get("description", ""),
                                path=bi_item.get("path", ""),
                            )
                            self._install_base_image(bi_path, bi_detail)
                            logger.info(f"Auto-installed base image '{bi_name}' for blueprint")
                        except Exception as e:
                            logger.warning(f"Could not auto-install base image '{bi_name}': {e}")
                    else:
                        logger.warning(
                            f"Required base image '{bi_name}' not found in catalog index"
                        )

        # Extract walkthrough -> create Content model if present
        content_id: Optional[UUID] = None
        walkthrough = blueprint_data.get("walkthrough")

        # Check for standalone content.json first (preferred for rich content)
        content_json_path = self._file_in(item_path, "content.json")
        if content_json_path.exists():
            try:
                with open(content_json_path, "r", encoding="utf-8") as f:
                    content_data = json.load(f)
                content_id = self._create_content_from_json(content_data, detail.name, user_id)
                # Auto-generate walkthrough_data from body_markdown if not
                # provided explicitly in content.json
                if (
                    content_id
                    and not content_data.get("walkthrough_data")
                    and content_data.get("body_markdown")
                ):
                    content_obj = self.db.query(Content).filter(Content.id == content_id).first()
                    if content_obj and not content_obj.walkthrough_data:
                        content_obj.walkthrough_data = parse_markdown_to_walkthrough(
                            content_obj.title or detail.name,
                            content_data["body_markdown"],
                        )
                        self.db.flush()
                        logger.info(
                            f"Auto-generated walkthrough_data from body_markdown "
                            f"for '{content_obj.title}'"
                        )

                # Also extract walkthrough_data for MSEL config
                if content_data.get("walkthrough_data"):
                    walkthrough = content_data["walkthrough_data"]
            except (IOError, json.JSONDecodeError) as e:
                logger.warning(f"Could not read content.json: {e}")

        # Fall back to walkthrough embedded in blueprint.yaml
        if not content_id and walkthrough:
            content_id = self._create_content_from_walkthrough(walkthrough, detail.name, user_id)

        # Build MSEL markdown from events or standalone msel.md
        msel_content: Optional[str] = None

        # Check for standalone msel.md first
        msel_md_path = self._file_in(item_path, "msel.md")
        if msel_md_path.exists():
            try:
                msel_content = msel_md_path.read_text(encoding="utf-8")
            except (IOError, UnicodeDecodeError) as e:
                logger.warning(f"Could not read msel.md: {e}")

        # Fall back to building MSEL from events in blueprint.yaml
        events = blueprint_data.get("events", [])
        if not msel_content and events:
            msel_content = self._build_msel_from_events(events)

        # Build the blueprint config from YAML structure
        config = build_config_from_yaml(blueprint_data, item_name=detail.name)

        # If we built MSEL content, make sure it's in the config
        if msel_content and config.get("msel"):
            config["msel"]["content"] = msel_content
        elif msel_content:
            config["msel"] = {
                "content": msel_content,
                "format": "markdown",
            }

        # Add walkthrough data to the msel config if present
        if walkthrough and config.get("msel"):
            config["msel"]["walkthrough"] = walkthrough
        elif walkthrough:
            config["msel"] = {
                "content": msel_content or "",
                "format": "yaml",
                "walkthrough": walkthrough,
            }

        # Set content_ids in config so blueprint links the content
        content_ids: List[str] = []
        if content_id:
            content_ids.append(str(content_id))
        if content_ids:
            config["content_ids"] = content_ids

        # Check for existing blueprint with same seed_id
        seed_id = blueprint_data.get("seed_id")
        if seed_id:
            existing = (
                self.db.query(RangeBlueprint).filter(RangeBlueprint.seed_id == seed_id).first()
            )
            if existing:
                logger.info(
                    f"Blueprint with seed_id '{seed_id}' already exists, " f"updating config"
                )
                existing.config = config
                existing.description = blueprint_data.get("description", existing.description)
                if content_ids:
                    existing.content_ids = content_ids
                self.db.flush()
                return existing.id

        # Create the RangeBlueprint
        blueprint = RangeBlueprint(
            name=blueprint_data.get("name", detail.name),
            description=blueprint_data.get("description", detail.description),
            base_subnet_prefix=blueprint_data.get("base_subnet_prefix", "10.0.0.0/8"),
            config=config,
            version=1,
            next_offset=0,
            is_seed=bool(seed_id),
            seed_id=seed_id,
            created_by=user_id,
            content_ids=content_ids,
        )
        self.db.add(blueprint)
        self.db.flush()

        logger.info(f"Created blueprint '{blueprint.name}' (id={blueprint.id})")
        return blueprint.id

    def _refuse_blueprint_from_the_other_era(
        self,
        blueprint_data: dict,
        detail: CatalogItemDetail,
    ) -> None:
        """Refuse a blueprint written for the other substrate, at install and not at deploy.

        One catalog serves both eras and an item is written for one of them. Installed on the
        wrong one it becomes a RangeBlueprint that lists, opens and offers a Deploy button like
        any other, and the refusal arrives later from a worker, after a range row already exists
        for the user to clean up. The message names the era so the answer is "this item is for the
        other substrate" rather than "deployment failed".

        Raises:
            ValueError: naming the item and the era. The install route turns it into a 400.
        """
        version = declared_schema_version(blueprint_data)
        substrate = self.settings.range_substrate
        if substrate == SUBSTRATE_KUBERNETES and version == SCHEMA_VERSION_LEGACY:
            raise ValueError(f"'{detail.name}' cannot be installed here. {ERA_A_ON_KUBERNETES}")
        if substrate == SUBSTRATE_DOCKER and version == SCHEMA_VERSION_K8S:
            raise ValueError(f"'{detail.name}' cannot be installed here. {ERA_B_ON_DOCKER}")

    def _create_content_from_walkthrough(
        self,
        walkthrough: dict,
        blueprint_name: str,
        user_id: UUID,
    ) -> Optional[UUID]:
        """Create a Content model from a walkthrough definition.

        Args:
            walkthrough: The walkthrough data from blueprint.yaml.
            blueprint_name: Name of the parent blueprint (for title fallback).
            user_id: User performing the install.

        Returns:
            UUID of the created Content, or None on failure.
        """
        title = walkthrough.get("title", f"{blueprint_name} - Student Guide")

        # Check if content with this title already exists
        existing = self.db.query(Content).filter(Content.title == title).first()
        if existing:
            logger.info(f"Content '{title}' already exists (id={existing.id})")
            return existing.id

        try:
            # Build body markdown from walkthrough phases for a simple text view
            body_parts = [f"# {title}\n"]
            if walkthrough.get("description"):
                body_parts.append(f"\n{walkthrough['description']}\n")

            for phase in walkthrough.get("phases", []):
                body_parts.append(f"\n## {phase.get('name', 'Phase')}\n")
                for step in phase.get("steps", []):
                    step_title = step.get("title", "Step")
                    body_parts.append(f"\n### {step_title}\n")
                    if step.get("content"):
                        body_parts.append(step["content"])

            body_markdown = "\n".join(body_parts)

            content = Content(
                title=title,
                description=walkthrough.get("description"),
                content_type=ContentType.STUDENT_GUIDE,
                body_markdown=body_markdown,
                walkthrough_data=walkthrough,
                version=walkthrough.get("version", "1.0"),
                tags=walkthrough.get("tags", []),
                created_by_id=user_id,
                is_published=True,  # Auto-publish catalog content
            )
            self.db.add(content)
            self.db.flush()

            logger.info(f"Created content '{title}' (id={content.id})")
            return content.id

        except Exception as e:
            logger.error(f"Failed to create content from walkthrough: {e}")
            return None

    def _create_content_from_json(
        self,
        content_data: dict,
        blueprint_name: str,
        user_id: UUID,
    ) -> Optional[UUID]:
        """Create a Content model from a content.json file.

        Args:
            content_data: The parsed content.json data.
            blueprint_name: Name of the parent blueprint (for title fallback).
            user_id: User performing the install.

        Returns:
            UUID of the created Content, or None on failure.
        """
        title = content_data.get("title", f"{blueprint_name} - Student Guide")

        # Check if content with this title already exists
        existing = self.db.query(Content).filter(Content.title == title).first()
        if existing:
            logger.info(f"Content '{title}' already exists (id={existing.id})")
            return existing.id

        try:
            # Determine content type
            content_type_str = content_data.get("content_type", "student_guide")
            try:
                content_type = ContentType(content_type_str)
            except ValueError:
                content_type = ContentType.STUDENT_GUIDE

            content = Content(
                title=title,
                description=content_data.get("description"),
                content_type=content_type,
                body_markdown=content_data.get("body_markdown", ""),
                walkthrough_data=content_data.get("walkthrough_data"),
                version=content_data.get("version", "1.0"),
                tags=content_data.get("tags", []),
                created_by_id=user_id,
                is_published=True,  # Auto-publish catalog content
            )
            self.db.add(content)
            self.db.flush()

            logger.info(f"Created content from content.json '{title}' (id={content.id})")
            return content.id

        except Exception as e:
            logger.error(f"Failed to create content from content.json: {e}")
            return None

    def _build_msel_from_events(self, events: List[dict]) -> str:
        """Build MSEL markdown content from a list of event dictionaries.

        Args:
            events: List of event dicts with sequence, delay_minutes, title, description.

        Returns:
            Markdown formatted MSEL content.
        """
        lines = ["# MSEL - Master Scenario Events List\n"]

        for event in events:
            seq = event.get("sequence", 0)
            delay = event.get("delay_minutes", 0)
            title = event.get("title", f"Event {seq}")
            description = event.get("description", "")

            hours = delay // 60
            minutes = delay % 60
            time_str = f"T+{hours:02d}:{minutes:02d}"

            lines.append(f"\n## {time_str} - {title}\n")
            if description:
                lines.append(f"\n{description}\n")

        return "\n".join(lines)

    def _install_scenario(
        self,
        item_path: Path,
        detail: CatalogItemDetail,
    ) -> None:
        """Install a scenario YAML file into the local scenarios directory.

        Copies the scenario YAML file to /data/scenarios/.

        Args:
            item_path: Path to the scenario YAML file.
            detail: The catalog item detail.

        Raises:
            ValueError: if the item id would name a file outside the scenario
                directory, or the file to copy lies outside the catalog.
        """
        from proving_ground.services.scenario_filesystem import get_scenarios_dir

        scenarios_dir = get_scenarios_dir()
        scenarios_dir.mkdir(parents=True, exist_ok=True)

        # Determine source file - item_path may be the YAML file or its directory
        if item_path.is_file() and item_path.suffix in (".yaml", ".yml"):
            src_file = item_path
        elif item_path.is_dir():
            # Look for a YAML file with the item name
            src_file = self._file_in(item_path, f"{detail.id}.yaml")
            if not src_file.exists():
                # Try any YAML file in the directory
                yaml_files = list(item_path.glob("*.yaml"))
                if yaml_files:
                    src_file = self._file_in(item_path, yaml_files[0].name)
                else:
                    raise FileNotFoundError(f"No YAML scenario file found in {item_path}")
        else:
            raise FileNotFoundError(f"Scenario file not found at {item_path}")

        if not src_file.exists():
            raise FileNotFoundError(f"Scenario file not found: {src_file}")

        # Copy to scenarios directory. The destination is resolved because the
        # item id comes from the catalog's index and becomes the filename.
        dest_file = resolve_declared_path(
            scenarios_dir,
            f"{detail.id}.yaml",
            what=f"scenario file for '{detail.id}'",
            root_label="the scenario directory",
        )
        shutil.copy2(str(src_file), str(dest_file))

        logger.info(f"Installed scenario '{detail.name}' to {dest_file}")

    def _install_image(
        self,
        item_path: Path,
        detail: CatalogItemDetail,
        build_images: bool,
    ) -> None:
        """Install a Docker image project from the catalog.

        Copies the Dockerfile project directory to /data/images/ and optionally
        builds the image. Registers a BaseImage record.

        Args:
            item_path: Path to the image project directory.
            detail: The catalog item detail.
            build_images: Whether to build the Docker image after copying.
        """
        if not item_path.is_dir():
            raise FileNotFoundError(f"Image project directory not found: {item_path}")

        project_name = detail.id
        self._install_image_from_path(item_path, project_name, build_images)

    def _install_image_from_path(
        self,
        src_dir: Path,
        project_name: str,
        build_images: bool,
    ) -> None:
        """Shared logic for installing a Dockerfile image project.

        Copies the project to /data/images/<project_name>/, optionally builds
        the Docker image, and registers a BaseImage record if one doesn't exist.

        Args:
            src_dir: Source directory containing the Dockerfile and related files.
            project_name: The project name (used as directory name and image tag).
            build_images: Whether to build the Docker image.

        Raises:
            ValueError: if the project name would write outside the image library.
        """
        images_dir = Path(IMAGES_DIR)
        images_dir.mkdir(parents=True, exist_ok=True)
        # The project name reaches here from the catalog index, either as an item
        # id or as an entry in a blueprint's requires_images, so it is resolved
        # rather than joined.
        dest_dir = resolve_declared_path(
            images_dir,
            project_name,
            what=f"image project '{project_name}'",
            root_label="the image library",
        )
        if dest_dir == images_dir.resolve():
            raise ValueError("image project name must not be empty")

        # Skip if already exists
        if dest_dir.exists():
            logger.info(
                f"Image project '{project_name}' already exists at {dest_dir}, " f"skipping copy"
            )
        else:
            # Copy the entire project directory. symlinks=True so a link the
            # catalog committed is copied as a link rather than dereferenced,
            # which would pull the contents of whatever it points at on this
            # host into the image library.
            shutil.copytree(str(src_dir), str(dest_dir), symlinks=True)
            logger.info(f"Copied image project '{project_name}' to {dest_dir}")

        # Build the image if requested. Tag under the configured image namespace
        # (default "proving-ground"); legacy "proving_ground/" is still recognized when
        # resolving images, so upstream cyroid-catalog content keeps working.
        image_tag = f"{self.settings.image_namespace}/{project_name}:latest"
        if build_images and (dest_dir / "Dockerfile").exists():
            self._build_docker_image(image_tag, dest_dir)

        # Register BaseImage if not already registered
        existing_image = (
            self.db.query(BaseImage).filter(BaseImage.image_project_name == project_name).first()
        )
        if not existing_image:
            # Read description from README.md if present. Both of these files
            # arrived with the copied project, so either can be a symlink the
            # catalog committed; a ValueError here means it points out of the
            # project and the file is treated as absent.
            description = f"Catalog image: {project_name}"
            try:
                readme_path = self._file_in_project(dest_dir, "README.md")
                if readme_path.exists():
                    readme_text = readme_path.read_text(encoding="utf-8")
                    # Use first paragraph as description
                    for line in readme_text.strip().split("\n"):
                        line = line.strip().lstrip("#").strip()
                        if line:
                            description = line[:200]
                            break
            except (IOError, UnicodeDecodeError, ValueError):
                pass

            # Read container_config from image.yaml if present
            container_config = None
            try:
                image_yaml_path = self._file_in_project(dest_dir, "image.yaml")
                if image_yaml_path.exists():
                    with open(image_yaml_path) as f:
                        image_meta = yaml.safe_load(f) or {}
                    if image_meta.get("container_config"):
                        container_config = image_meta["container_config"]
                        logger.info(
                            f"Image '{project_name}': loaded container_config "
                            f"from image.yaml: {container_config}"
                        )
            except (IOError, yaml.YAMLError, ValueError) as exc:
                logger.warning(f"Failed to read image.yaml for '{project_name}': {exc}")

            base_image = BaseImage(
                name=project_name,
                description=description,
                image_type=ImageType.CONTAINER.value,
                docker_image_tag=image_tag,
                image_project_name=project_name,
                os_type="linux",
                vm_type="container",
                native_arch="x86_64",
                is_global=True,
                created_by=None,
                container_config=container_config,
            )
            self.db.add(base_image)
            self.db.flush()
            logger.info(f"Registered BaseImage '{project_name}' (tag={image_tag})")

    def _build_docker_image(self, image_tag: str, project_dir: Path) -> bool:
        """Build a Docker image from a project directory.

        If a local registry is available, checks if the image already exists there
        and skips the build. After building, pushes to the registry and cleans up
        from the host Docker daemon.

        Args:
            image_tag: The tag for the built image.
            project_dir: Directory containing the Dockerfile.

        Returns:
            True if build succeeded (or image already in registry), False otherwise.

        Raises:
            RegistryPushError: If pushing to registry fails.
        """
        try:
            import docker

            client = docker.from_env()
            registry = get_registry_service()

            # Check if image already exists in registry (skip build if so)
            loop = asyncio.new_event_loop()
            try:
                registry_healthy = loop.run_until_complete(registry.is_healthy())
                if registry_healthy:
                    image_in_registry = loop.run_until_complete(registry.image_exists(image_tag))
                    if image_in_registry:
                        logger.info(
                            f"Image {image_tag} already exists in registry, " f"skipping build"
                        )
                        return True
            finally:
                loop.close()

            # Check if image already exists on host
            try:
                client.images.get(image_tag)
                logger.info(f"Image {image_tag} already exists on host, skipping build")
                # Image exists on host but not in registry - still need to push
            except docker.errors.ImageNotFound:
                # Build the image
                logger.info(f"Building image {image_tag} from {project_dir}")
                image, build_logs = client.images.build(
                    path=str(project_dir),
                    tag=image_tag,
                    rm=True,
                    forcerm=True,
                )

                for log_line in build_logs:
                    if "stream" in log_line:
                        logger.debug(log_line["stream"].strip())

                logger.info(f"Successfully built image {image_tag}")

            # Push to registry and cleanup from host
            loop = asyncio.new_event_loop()
            try:
                registry_healthy = loop.run_until_complete(registry.is_healthy())
                if registry_healthy:
                    logger.info(f"Pushing {image_tag} to registry and cleaning up host")
                    # push_and_cleanup raises RegistryPushError on failure
                    loop.run_until_complete(registry.push_and_cleanup(image_tag))
                    logger.info(f"Image {image_tag} pushed to registry and removed from host")
                else:
                    logger.warning(f"Registry not healthy, keeping {image_tag} on host only")
            finally:
                loop.close()

            return True

        except RegistryPushError:
            # Re-raise registry push errors to fail the operation
            raise
        except Exception as e:
            logger.error(f"Failed to build image {image_tag}: {e}")
            return False

    def _install_base_image(
        self,
        item_path: Path,
        detail: CatalogItemDetail,
    ) -> Optional[UUID]:
        """Install a base image from the catalog by reading its YAML and registering a BaseImage.

        Base images define VM types (container, linux_vm, windows_vm) and their
        Docker images. The YAML is parsed and a corresponding BaseImage record
        is created.

        Args:
            item_path: Path to the base image YAML file.
            detail: The catalog item detail.

        Returns:
            UUID of the created or existing BaseImage, or None on failure.
        """
        # Determine the YAML file
        if item_path.is_file() and item_path.suffix in (".yaml", ".yml"):
            yaml_path = item_path
        elif item_path.is_dir():
            yaml_path = self._file_in(item_path, f"{detail.id}.yaml")
            if not yaml_path.exists():
                yaml_files = list(item_path.glob("*.yaml"))
                if yaml_files:
                    yaml_path = self._file_in(item_path, yaml_files[0].name)
                else:
                    raise FileNotFoundError(f"No YAML base image file found in {item_path}")
        else:
            raise FileNotFoundError(f"Base image file not found at {item_path}")

        if not yaml_path.exists():
            raise FileNotFoundError(f"Base image file not found: {yaml_path}")

        with open(yaml_path, "r", encoding="utf-8") as f:
            base_image_data = yaml.safe_load(f)

        if not base_image_data:
            raise ValueError(f"Empty base image file: {yaml_path}")

        # Extract fields
        name = base_image_data.get("name", detail.name)
        description = base_image_data.get("description", detail.description)
        vm_type = base_image_data.get("vm_type", "container")
        os_type = base_image_data.get("os_type", "linux")
        base_image_tag = base_image_data.get("base_image", "")
        native_arch = base_image_data.get("native_arch", "x86_64")
        default_cpu = base_image_data.get("default_cpu", 2)
        default_ram_mb = base_image_data.get("default_ram_mb", 4096)
        default_disk_gb = base_image_data.get("default_disk_gb", 40)
        tags = base_image_data.get("tags", [])

        # Determine image type from vm_type
        if vm_type in ("container",):
            image_type = ImageType.CONTAINER.value
        else:
            image_type = ImageType.ISO.value

        # Check if already exists by name or docker_image_tag
        existing = (
            self.db.query(BaseImage)
            .filter(
                (BaseImage.name == name)
                | (BaseImage.docker_image_tag == base_image_tag if base_image_tag else False)
            )
            .first()
        )
        if existing:
            logger.info(f"BaseImage '{name}' already exists (id={existing.id}), skipping")
            return existing.id

        # Create BaseImage
        base_image = BaseImage(
            name=name,
            description=description[:500] if description else None,
            image_type=image_type,
            docker_image_tag=base_image_tag if base_image_tag else None,
            os_type=os_type,
            vm_type=vm_type,
            native_arch=native_arch,
            default_cpu=default_cpu,
            default_ram_mb=default_ram_mb,
            default_disk_gb=default_disk_gb,
            tags=tags,
            is_global=True,
            created_by=None,
        )
        self.db.add(base_image)
        self.db.flush()

        logger.info(
            f"Installed BaseImage '{name}' "
            f"(type={image_type}, vm_type={vm_type}, id={base_image.id})"
        )
        return base_image.id

    def _install_content(
        self,
        item_path: Path,
        detail: CatalogItemDetail,
        user_id: UUID,
    ) -> Optional[UUID]:
        """Install a content item from the catalog.

        Args:
            item_path: Path to the content directory or file.
            detail: The catalog item detail.
            user_id: User performing the install.

        Returns:
            UUID of the created Content, or None on failure.
        """
        # Look for markdown content
        if item_path.is_file():
            content_text = item_path.read_text(encoding="utf-8")
        elif item_path.is_dir():
            # Try common content filenames
            for filename in ("content.md", "README.md", "guide.md"):
                content_file = self._file_in(item_path, filename)
                if content_file.exists():
                    content_text = content_file.read_text(encoding="utf-8")
                    break
            else:
                raise FileNotFoundError(f"No content file found in {item_path}")
        else:
            raise FileNotFoundError(f"Content path not found: {item_path}")

        # Check for existing content with same title
        existing = self.db.query(Content).filter(Content.title == detail.name).first()
        if existing:
            logger.info(f"Content '{detail.name}' already exists (id={existing.id})")
            return existing.id

        content = Content(
            title=detail.name,
            description=detail.description,
            content_type=ContentType.CUSTOM,
            body_markdown=content_text,
            version=detail.version,
            tags=detail.tags,
            created_by_id=user_id,
            is_published=False,
        )
        self.db.add(content)
        self.db.flush()

        logger.info(f"Installed content '{detail.name}' (id={content.id})")
        return content.id

    # =========================================================================
    # Uninstall
    # =========================================================================

    def uninstall_item(self, installed_item: CatalogInstalledItem) -> None:
        """Uninstall an item that was previously installed from a catalog source.

        For blueprints, deletes the RangeBlueprint (if it has no instances).
        For scenarios, deletes the scenario YAML file.
        For other types, only removes the installed item record.

        Args:
            installed_item: The installed item record to uninstall.
        """
        item_type = installed_item.item_type
        resource_id = installed_item.local_resource_id

        if item_type == CatalogItemType.BLUEPRINT and resource_id:
            # Delete the blueprint if it has no range instances
            blueprint = (
                self.db.query(RangeBlueprint).filter(RangeBlueprint.id == resource_id).first()
            )
            if blueprint:
                if blueprint.instances:
                    raise ValueError(
                        f"Cannot uninstall blueprint '{blueprint.name}': "
                        f"it has {len(blueprint.instances)} active range instances"
                    )
                self.db.delete(blueprint)
                logger.info(f"Deleted blueprint '{blueprint.name}'")

        elif item_type == CatalogItemType.SCENARIO:
            # Delete the scenario YAML file
            from proving_ground.services.scenario_filesystem import (
                delete_scenario,
            )

            deleted = delete_scenario(installed_item.catalog_item_id)
            if deleted:
                logger.info(f"Deleted scenario file for '{installed_item.item_name}'")
            else:
                logger.warning(f"Scenario file for '{installed_item.item_name}' not found")

        elif item_type == CatalogItemType.CONTENT and resource_id:
            content = self.db.query(Content).filter(Content.id == resource_id).first()
            if content:
                self.db.delete(content)
                logger.info(f"Deleted content '{content.title}'")

        # Note: IMAGE and TEMPLATE items leave their BaseImage/files in place
        # since other blueprints may reference them. Only the tracking record
        # is removed.

        # Delete the installed item record
        self.db.delete(installed_item)
        self.db.commit()

        logger.info(
            f"Uninstalled catalog item '{installed_item.item_name}' " f"(type={item_type.value})"
        )

    def resolve_blueprint_origin(self, blueprint_id: UUID) -> Optional["CatalogOrigin"]:
        """Find the catalog item a blueprint was installed from, and read its YAML.

        Returns None when the blueprint was authored locally, when its catalog
        source has been removed, or when the source is no longer synced -- all
        of which mean there is nothing to contribute back to.

        Raises:
            ValueError: if the catalog still lists the item but its
                blueprint.yaml is missing or unparseable, which is a broken
                catalog rather than an absent one.
        """
        installed = (
            self.db.query(CatalogInstalledItem)
            .filter(
                CatalogInstalledItem.local_resource_id == blueprint_id,
                CatalogInstalledItem.item_type == CatalogItemType.BLUEPRINT,
            )
            .first()
        )
        if not installed:
            return None

        source = (
            self.db.query(CatalogSource)
            .filter(CatalogSource.id == installed.catalog_source_id)
            .first()
        )
        if not source:
            return None

        catalog_root = self._get_catalog_root(source)
        if not catalog_root.exists():
            return None

        try:
            index = self._load_index(source)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

        item_data = next(
            (i for i in index.get("items", []) if i.get("id") == installed.catalog_item_id),
            None,
        )
        if not item_data:
            return None

        item_dir = item_data.get("path", "")
        # index.json is catalog-supplied content, so its paths are not trusted:
        # a '../..' would read outside the catalog root.
        item_root = resolve_declared_path(
            catalog_root,
            item_dir,
            what=f"catalog item '{installed.catalog_item_id}' path",
        )
        yaml_path = self._file_in(item_root, "blueprint.yaml")
        if not yaml_path.exists():
            raise ValueError(
                f"catalog item '{installed.catalog_item_id}' is indexed but its "
                f"blueprint.yaml is missing at {yaml_path}"
            )

        text = yaml_path.read_text(encoding="utf-8")
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(
                f"catalog item '{installed.catalog_item_id}' has an unparseable "
                f"blueprint.yaml: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise ValueError(
                f"catalog item '{installed.catalog_item_id}' blueprint.yaml is not a mapping"
            )

        return CatalogOrigin(
            source_id=source.id,
            source_name=source.name,
            source_url=source.url,
            source_branch=source.branch,
            item_id=installed.catalog_item_id,
            item_name=installed.item_name,
            installed_version=installed.installed_version,
            item_path=str(Path(item_dir) / "blueprint.yaml"),
            yaml_path=yaml_path,
            document=document,
            text=text,
        )
