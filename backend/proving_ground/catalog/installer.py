"""Install a catalog blueprint and everything it depends on, in one go (PG-149).

`CatalogService.install_item` installs one item. This drives it: it resolves the
blueprint's dependencies into an ordered plan, runs each step, reports progress
as it goes, and fails with the name of the step that broke rather than a
traceback from three frames down.

The resolution lives in `dependencies`; this module is the part that touches the
catalog on disk and the database, so it stays deliberately thin -- gather the
facts, hand them to the resolver, then walk the plan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Set
from uuid import UUID

import yaml

from proving_ground.services.catalog_service import resolve_declared_path
from proving_ground.catalog.dependencies import (
    BASE_IMAGE,
    BLUEPRINT,
    CONTENT,
    IMAGE,
    Dependency,
    DependencyInstallError,
    InstallPlan,
    resolve_install_plan,
)
from proving_ground.models.base_image import BaseImage
from proving_ground.models.blueprint import RangeBlueprint
from proving_ground.models.catalog import CatalogInstalledItem

logger = logging.getLogger(__name__)

# (step, completed_count, total) -- completed counts steps finished, so the
# first call is (step, 0, total).
ProgressCallback = Callable[[Dependency, int, int], None]
CancelledCheck = Callable[[], bool]


@dataclass
class InstallOutcome:
    """What a one-click install actually did."""

    blueprint_id: Optional[UUID] = None
    installed: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    content_ids: List[str] = field(default_factory=list)
    cancelled: bool = False


class CatalogInstaller:
    """Resolve and install a blueprint together with its dependencies."""

    def __init__(self, service, progress: Optional[ProgressCallback] = None):
        self.service = service
        self.db = service.db
        self._progress = progress

    # --- planning ---------------------------------------------------------

    def build_plan(self, source, item_id: str) -> InstallPlan:
        """Resolve what installing ``item_id`` from ``source`` will involve.

        Raises:
            ValueError: if the item is not in the catalog index, or is not a
                blueprint -- the other types have no dependency graph.
        """
        index = self.service._load_index(source)
        items = index.get("items") or []
        item = next((i for i in items if i.get("id") == item_id), None)
        if item is None:
            raise ValueError(f"Item '{item_id}' not found in catalog source")
        if item.get("type") != BLUEPRINT:
            raise ValueError(
                f"Item '{item_id}' is a {item.get('type')}, and only blueprints "
                "have dependencies to resolve"
            )

        catalog_root = self.service._get_catalog_root(source)
        # index.json comes from a remote repository, so the path it declares is
        # attacker-controlled. Unresolved, `_ships_content` stats and parses whatever it points
        # at -- and this route takes any authenticated caller, so it answers "does this file
        # exist" for any path the API can reach.
        item_dir = resolve_declared_path(
            catalog_root, item.get("path", ""), what=f"item '{item_id}' path"
        )

        return resolve_install_plan(
            item,
            index_items=items,
            installed_item_ids=self._installed_ids(source),
            available_image_dirs=self._buildable_images(catalog_root),
            installed_image_projects=self._installed_image_projects(),
            has_content=self._ships_content(item_dir),
        )

    def _installed_ids(self, source) -> Set[str]:
        rows = (
            self.db.query(CatalogInstalledItem.catalog_item_id)
            .filter(CatalogInstalledItem.catalog_source_id == source.id)
            .all()
        )
        return {row[0] for row in rows}

    def _installed_image_projects(self) -> Set[str]:
        """Dockerfile projects already registered locally.

        These are not catalog index items, so they leave no
        CatalogInstalledItem behind; the BaseImage row is what makes a re-run
        skip them instead of copying and rebuilding.
        """
        rows = (
            self.db.query(BaseImage.image_project_name)
            .filter(BaseImage.image_project_name.isnot(None))
            .all()
        )
        return {row[0] for row in rows if row[0]}

    def _buildable_images(self, catalog_root: Path) -> Set[str]:
        """Image projects the catalog can actually build, by directory name."""
        images_dir = catalog_root / "images"
        if not images_dir.is_dir():
            return set()
        return {
            child.name
            for child in images_dir.iterdir()
            if child.is_dir() and (child / "Dockerfile").exists()
        }

    @staticmethod
    def _ships_content(item_dir: Path) -> bool:
        if (item_dir / "content.json").exists():
            return True
        blueprint_yaml = item_dir / "blueprint.yaml"
        if not blueprint_yaml.exists():
            return False
        try:
            data = yaml.safe_load(blueprint_yaml.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            return False
        return bool(isinstance(data, dict) and data.get("walkthrough"))

    # --- execution --------------------------------------------------------

    def install(
        self,
        source,
        item_id: str,
        user_id: UUID,
        *,
        build_images: bool = True,
        plan: Optional[InstallPlan] = None,
        is_cancelled: Optional[CancelledCheck] = None,
    ) -> InstallOutcome:
        """Run the install plan, reporting each step.

        Raises:
            DependencyInstallError: naming the step that failed. Steps already
                completed are left in place; this is not a transaction, because
                a half-installed base image is still a usable base image.
        """
        plan = plan or self.build_plan(source, item_id)
        outcome = InstallOutcome(
            missing=[d.key for d in plan.missing],
            warnings=list(plan.warnings),
        )
        catalog_root = self.service._get_catalog_root(source)
        total = plan.total_steps
        done = 0

        for step in plan.steps:
            if not step.available:
                continue
            if step.satisfied:
                outcome.skipped.append(step.key)
                continue
            if is_cancelled and is_cancelled():
                outcome.cancelled = True
                return outcome

            self._report(step, done, total)
            try:
                self._run_step(step, source, catalog_root, user_id, build_images, outcome)
            except DependencyInstallError:
                raise
            except Exception as exc:  # noqa: BLE001 - re-raised with the step's name
                logger.exception("catalog install step %s failed", step.key)
                raise DependencyInstallError(step, exc) from exc

            outcome.installed.append(step.key)
            done += 1

        return outcome

    def _run_step(
        self,
        step: Dependency,
        source,
        catalog_root: Path,
        user_id: UUID,
        build_images: bool,
        outcome: InstallOutcome,
    ) -> None:
        if step.kind == BASE_IMAGE:
            # Through install_item, not _install_base_image directly: the former
            # also records the CatalogInstalledItem, which is what makes a
            # second run skip this step rather than reinstall it.
            self.service.install_item(
                source=source,
                item_id=step.ref,
                user_id=user_id,
                build_images=build_images,
            )
            return

        if step.kind == IMAGE:
            # The copy SOURCE, which the one-click installer is the only route to: the
            # dependency-resolving path calls install_item with install_dependencies=False, so
            # the guarded loop in _install_blueprint never runs for these steps. A directory
            # symlinked out of the catalog is copied in with its contents intact.
            self.service._install_image_from_path(
                resolve_declared_path(catalog_root, step.path, what=f"image '{step.ref}' path"),
                step.ref,
                build_images,
            )
            return

        if step.kind == BLUEPRINT:
            installed = self.service.install_item(
                source=source,
                item_id=step.ref,
                user_id=user_id,
                build_images=build_images,
                install_dependencies=False,
            )
            outcome.blueprint_id = installed.local_resource_id
            return

        if step.kind == CONTENT:
            # Content is created by the blueprint installer as it builds the
            # blueprint's config, so this step verifies rather than installs --
            # it exists so a blueprint whose content silently failed to attach
            # is visible instead of reported as a clean install.
            self._verify_content(outcome)
            return

        raise ValueError(f"unsupported install step kind {step.kind!r}")

    def _verify_content(self, outcome: InstallOutcome) -> None:
        if outcome.blueprint_id is None:
            raise ValueError("content step ran before the blueprint was installed")
        blueprint = (
            self.db.query(RangeBlueprint).filter(RangeBlueprint.id == outcome.blueprint_id).first()
        )
        content_ids = list((blueprint.content_ids if blueprint else None) or [])
        if not content_ids:
            raise ValueError(
                "the catalog item ships content but none was attached to the blueprint"
            )
        outcome.content_ids = [str(cid) for cid in content_ids]

    def _report(self, step: Dependency, done: int, total: int) -> None:
        if self._progress:
            self._progress(step, done, total)


__all__ = ["CatalogInstaller", "InstallOutcome", "DependencyInstallError"]
