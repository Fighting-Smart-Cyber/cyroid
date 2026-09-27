"""The Era B branch of the range endpoints -- COSMOS PG-41/PG-42/PG-58.

`api/ranges.py` and `api/blueprints.py` are Era A code, frozen at v0.45.0, and every one of
their range operations drives DinD directly. On a host with `range_substrate=kubernetes` those
operations must go to the cluster instead -- a delete that tore down a DinD container that does
not exist and then dropped the row would leave the range's namespace, VMs and volumes running
with nothing pointing at them.

So each endpoint asks `is_kubernetes()` first and hands the request here. Small on purpose: the
endpoints keep their authorisation and status checks, and this turns the service's answers into
HTTP ones. The endpoints are synchronous (FastAPI runs them in a thread), so the async service
calls run on their own loop, exactly as `delete_range` already does for DinD.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from proving_ground.config import get_settings
from proving_ground.models.event_log import EventType
from proving_ground.models.range import Range, RangeStatus
from proving_ground.services import kubernetes_range_service as k8s

logger = logging.getLogger(__name__)

__all__ = [
    "delete_on_kubernetes",
    "destroy_for_cleanup",
    "is_kubernetes",
    "start_on_kubernetes",
    "stop_on_kubernetes",
    "teardown_on_kubernetes",
    "validate_for_deploy",
]


def is_kubernetes() -> bool:
    return get_settings().range_substrate == "kubernetes"


def validate_for_deploy(db: Session, range_id: UUID) -> None:
    """A 400 now beats an ERROR status ten seconds later from the worker."""
    try:
        k8s.validate_range_for_kubernetes(db, range_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def stop_on_kubernetes(db: Session, range_obj: Range, user_id: UUID | None = None) -> Range:
    """Record the intent to stop and answer; the worker drives the cluster. See `_dispatch`."""
    return _dispatch(db, range_obj, "stop", user_id)


def start_on_kubernetes(db: Session, range_obj: Range, user_id: UUID | None = None) -> Range:
    """Record the intent to start and answer; the worker drives the cluster. See `_dispatch`."""
    return _dispatch(db, range_obj, "start", user_id)


def _dispatch(db: Session, range_obj: Range, operation: str, user_id: UUID | None) -> Range:
    """Hand a lifecycle operation to the deploy worker and answer as soon as it is recorded.

    Both of these ran inline before. `start` waits for every machine to report Running, which is
    bounded at ten minutes, and it held an anyio threadpool worker for all of it: the browser has
    no timeout of its own, and any proxy in front of the API returns 504 long before the cluster
    is finished -- so the UI reported a failure about a range that was in fact coming up, and the
    obvious response, clicking Start again, cost a second worker. Deploy already answers this way
    and the range's status is what the UI follows.

    The range goes to DEPLOYING because that is the only transitional status the model has; it is
    what makes the progress panel appear, and the panel reads the operation out of the events
    rather than guessing it from the status.
    """
    from proving_ground.tasks.deployment import (
        START_STAGES,
        STOP_STAGES,
        start_range_task,
        stop_range_task,
    )

    # A blueprint the cluster path cannot read is a 400 now rather than an ERROR status a second
    # later from a worker nobody is watching -- the same reason `deploy` validates before it
    # dispatches.
    validate_for_deploy(db, range_obj.id)

    # Both actors are defined in `tasks/deployment.py`, which `proving_ground.tasks` imports --
    # importing the module is what registers an actor, so the worker knows them and neither
    # message can land on a queue nobody consumes.
    stages, actor = (
        (START_STAGES, start_range_task) if operation == "start" else (STOP_STAGES, stop_range_task)
    )
    range_obj.status = RangeStatus.DEPLOYING
    range_obj.error_message = None
    db.commit()
    _log_step(db, range_obj, operation, stages, user_id)
    actor.send(str(range_obj.id), str(user_id) if user_id else None)
    db.refresh(range_obj)
    return range_obj


def _log_step(
    db: Session, range_obj: Range, operation: str, stages: tuple[str, ...], user_id: UUID | None
) -> None:
    """The first stage, written here so the panel has something the moment the request returns.

    It also records who asked: the worker knows the operation but not the person, and a range
    that was stopped with nothing in its activity log is the defect this closes.
    """
    from proving_ground.services.event_service import EventService

    try:
        EventService(db).log_event(
            range_id=range_obj.id,
            event_type=EventType.DEPLOYMENT_STEP,
            message=f"[Stage 1/{len(stages)}] {stages[0]}",
            user_id=user_id,
            extra_data=json.dumps(
                {
                    "substrate": "kubernetes",
                    "operation": operation,
                    "stage": 1,
                    "total_stages": len(stages),
                    "stage_name": stages[0],
                }
            ),
        )
    except Exception as exc:  # noqa: BLE001 - the operation is dispatched either way
        logger.warning(
            "range %s: could not record the %s request: %s", range_obj.id, operation, exc
        )


def teardown_on_kubernetes(db: Session, range_obj: Range, user_id: UUID | None = None) -> Range:
    """Destroy the placement. Synchronous, unlike start and stop: the caller has to know whether
    the namespace actually went, because a teardown that left residue must not report DRAFT."""
    from proving_ground.services.event_service import EventService

    result = _run(k8s.destroy_range_on_kubernetes(db, range_obj.id), "teardown")
    logger.info("range %s torn down on kubernetes: %s", range_obj.id, result)
    range_obj.status = RangeStatus.DRAFT
    range_obj.error_message = None
    range_obj.stopped_at = datetime.now(timezone.utc)
    db.commit()
    try:
        EventService(db).log_event(
            range_id=range_obj.id,
            event_type=EventType.RANGE_TEARDOWN,
            message=(
                f"Range '{range_obj.name}' torn down from {result['namespace']} "
                f"-- residue: {result['residue']}"
            ),
            user_id=user_id,
            extra_data=json.dumps({"substrate": "kubernetes", "operation": "teardown", **result}),
        )
    except Exception as exc:  # noqa: BLE001 - the range is already down; the record is secondary
        logger.warning("range %s: could not record the teardown: %s", range_obj.id, exc)
    db.refresh(range_obj)
    return range_obj


def delete_on_kubernetes(db: Session, range_obj: Range) -> None:
    """Destroy first, delete the row only if that left nothing. The row is the only thing that
    remembers which namespace this range is, so dropping it after a failed destroy orphans the
    namespace for good."""
    _run(k8s.destroy_range_on_kubernetes(db, range_obj.id), "delete")


def destroy_for_cleanup(db: Session, range_obj: Range) -> str | None:
    """Destroy one range, answering with why it did not come down rather than raising.

    The bulk teardowns -- the admin purge, and ending a training event -- walk many ranges, and
    one that refuses must not abandon the rest, so the failure has to arrive as a value. Every
    failure counts, not only residue: the range row is the only record of which namespace a range
    is, so a destroy that was not proven clean must leave that row where it is.
    """
    try:
        result = asyncio.run(k8s.destroy_range_on_kubernetes(db, range_obj.id))
    except Exception as exc:
        logger.error("kubernetes teardown of range %s failed: %s", range_obj.id, exc)
        return str(exc)
    logger.info("range %s destroyed on kubernetes: %s", range_obj.id, result)
    return None


def _run(coro, operation: str):
    try:
        return asyncio.run(coro)
    except ValueError as exc:
        # The blueprint or placement is the problem, and that is the caller's to fix.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except (RuntimeError, TimeoutError) as exc:
        # Residue, a VM that failed, a wait that expired: the cluster's state is the finding.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"kubernetes {operation} failed: {exc}",
        ) from exc
