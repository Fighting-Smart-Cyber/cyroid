# backend/proving_ground/tasks/catalog_install.py
"""One-click catalog install as a background job (PG-149).

Installing a blueprint can mean pulling base images and building containers, so
it does not belong in a request. This runs the resolved plan on a worker and
reports each step through the shared job store, which is what the progress
modal polls.
"""

import logging
from typing import Any, Dict, Optional
from uuid import UUID

import dramatiq

from proving_ground.catalog.dependencies import Dependency, DependencyInstallError
from proving_ground.catalog.installer import CatalogInstaller
from proving_ground.database import get_session_local
from proving_ground.models.catalog import CatalogSource
from proving_ground.services.catalog_service import CatalogService
from proving_ground.tasks.jobs import CANCELLED, COMPLETED, FAILED, JobLog, JobStore

logger = logging.getLogger(__name__)

store = JobStore("catalog_install:")


def get_job_status(job_id: str) -> Optional[Dict[str, Any]]:
    return store.get(job_id)


def cancel_job(job_id: str) -> bool:
    return store.cancel(job_id)


@dramatiq.actor(max_retries=0, time_limit=1800000)  # 30 min, no retries
def install_catalog_item_async(
    job_id: str,
    source_id: str,
    item_id: str,
    user_id: str,
    build_images: bool = True,
) -> None:
    """Resolve and install a catalog blueprint with all of its dependencies."""
    db = get_session_local()()
    log = JobLog(store, job_id)

    try:
        source = db.query(CatalogSource).filter(CatalogSource.id == UUID(source_id)).first()
        if not source:
            log.say("Install failed", status=FAILED, error="Catalog source not found")
            return

        def report(step: Dependency, done: int, total: int) -> None:
            log.completed = done
            log.say(step.label, current_item=step.name, level="step")

        installer = CatalogInstaller(CatalogService(db), progress=report)
        log.say("Resolving dependencies...")

        try:
            plan = installer.build_plan(source, item_id)
        except ValueError as exc:
            log.say("Install failed", status=FAILED, error=str(exc))
            return

        log.total_steps = plan.total_steps
        log.say(f"Install plan: {plan.describe()}")
        for warning in plan.warnings:
            log.say(warning, level="warning")
        for skipped in plan.satisfied:
            log.say(f"Skipping {skipped.name} ({skipped.note})", level="skip")

        try:
            outcome = installer.install(
                source,
                item_id,
                UUID(user_id),
                build_images=build_images,
                plan=plan,
                is_cancelled=lambda: store.is_cancelled(job_id),
            )
        except DependencyInstallError as exc:
            db.rollback()
            log.say(
                str(exc),
                status=FAILED,
                level="error",
                current_item=exc.dependency.name,
                error=str(exc),
                result={"failed_step": exc.dependency.key},
            )
            return

        if outcome.cancelled:
            log.say("Install cancelled", status=CANCELLED, level="warning")
            return

        db.commit()
        log.completed = plan.total_steps
        log.say(
            "Install complete",
            status=COMPLETED,
            result={
                "blueprint_id": str(outcome.blueprint_id) if outcome.blueprint_id else None,
                "installed": outcome.installed,
                "skipped": outcome.skipped,
                "missing": outcome.missing,
                "warnings": outcome.warnings,
                "content_ids": outcome.content_ids,
            },
        )
    except Exception as exc:  # noqa: BLE001 - the job must record why it died
        logger.exception("catalog install job %s failed", job_id)
        db.rollback()
        log.say("Install failed", status=FAILED, level="error", error=str(exc))
    finally:
        db.close()


__all__ = ["install_catalog_item_async", "get_job_status", "cancel_job", "store"]
