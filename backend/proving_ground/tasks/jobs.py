# backend/proving_ground/tasks/jobs.py
"""Redis-backed progress for long-running jobs.

Blueprint import grew its own copy of this: a key prefix, a TTL, and four
functions over `setex`. The catalog one-click install (PG-149) needs exactly
the same thing, and two copies of a status contract is how a frontend ends up
polling one shape and receiving another. One implementation, parameterised by
key prefix.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from redis import Redis

from proving_ground.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_TTL = 3600  # an hour is long enough to finish and be polled afterwards

# The states a job moves through. Terminal states are the ones a poller can
# stop on.
PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = (COMPLETED, FAILED, CANCELLED)


class JobStore:
    """Progress for one family of jobs, namespaced by key prefix."""

    def __init__(self, prefix: str, ttl: int = DEFAULT_TTL):
        self.prefix = prefix
        self.ttl = ttl

    def _redis(self) -> Redis:
        return Redis.from_url(get_settings().redis_url, decode_responses=True)

    def key(self, job_id: str) -> str:
        return f"{self.prefix}{job_id}"

    def update(
        self,
        job_id: str,
        status: str,
        step: str,
        progress: int = 0,
        total_steps: int = 0,
        current_item: str = "",
        error: str = "",
        result: Optional[Dict[str, Any]] = None,
        log: Optional[list] = None,
    ) -> None:
        """Write the job's current state, replacing whatever was there."""
        payload: Dict[str, Any] = {
            "status": status,
            "step": step,
            "progress": progress,
            "total_steps": total_steps,
            "current_item": current_item,
            "error": error,
            "result": result,
            "updated_at": datetime.utcnow().isoformat(),
        }
        if log is not None:
            payload["log"] = log
        self._redis().setex(self.key(job_id), self.ttl, json.dumps(payload))

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        data = self._redis().get(self.key(job_id))
        return json.loads(data) if data else None

    def is_cancelled(self, job_id: str) -> bool:
        status = self.get(job_id)
        return status is not None and status.get("status") == CANCELLED

    def cancel(self, job_id: str) -> bool:
        """Mark a job cancelled. Only pending or running jobs can be cancelled."""
        status = self.get(job_id)
        if not status or status.get("status") not in (PENDING, RUNNING):
            return False
        self.update(
            job_id,
            CANCELLED,
            "Cancelled",
            progress=status.get("progress", 0),
            total_steps=status.get("total_steps", 0),
            log=status.get("log"),
        )
        return True

    def delete(self, job_id: str) -> None:
        self._redis().delete(self.key(job_id))


class JobLog:
    """The running log a progress modal shows, kept with the job's status.

    Held in memory by the worker and written out on every update, so a poller
    that connects late still sees the whole story rather than the last line.
    """

    def __init__(self, store: JobStore, job_id: str, total_steps: int = 0):
        self.store = store
        self.job_id = job_id
        self.total_steps = total_steps
        self.completed = 0
        self.lines: list = []

    def say(
        self,
        message: str,
        *,
        status: str = RUNNING,
        progress: Optional[int] = None,
        current_item: str = "",
        level: str = "info",
        error: str = "",
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.lines.append({"message": message, "level": level, "at": datetime.utcnow().isoformat()})
        self.store.update(
            self.job_id,
            status,
            message,
            progress=self.completed if progress is None else progress,
            total_steps=self.total_steps,
            current_item=current_item,
            error=error,
            result=result,
            log=self.lines,
        )
