"""What the gateway knows about published applications, and where it learns it.

A range's applications are declared as ordinary `networking.k8s.io/v1` Ingress objects naming an
ingress class **no controller serves** -- `pg-gateway`. That choice is deliberate:

  * the table is declarative and lives with the range, so it is garbage-collected when the
    range's namespace goes, with no residue to reconcile;
  * it is visible to `kubectl get ing -A` like anything else, so "what is published" is
    answerable without reading the platform's database;
  * and the gateway needs one cluster-wide read-only permission, rather than a database
    credential -- which is the whole argument for this process holding as little as possible.

Refreshed on a timer rather than watched. A watch is the better mechanism at scale and costs
reconnect handling, resourceVersion bookkeeping and a failure mode where a silently dead watch
looks exactly like an empty cluster. The table is one object per published application; a list
is cheap and its failure mode is "briefly stale", which for publishing an application is fine.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Mapping, Optional
from uuid import UUID

from proving_ground.gateway.routing import Route
from proving_ground.utils.range_hosts import range_id_from_key

logger = logging.getLogger(__name__)

# Defined by the producer -- capability/lifecycle.py writes these objects -- so the class name
# and the label keys have one definition rather than two that must agree.
from proving_ground.capability.lifecycle import (  # noqa: E402
    APP_LABEL,
    GATEWAY_INGRESS_CLASS,
    RANGE_LABEL,
)

__all__ = [
    "APP_LABEL",
    "GATEWAY_INGRESS_CLASS",
    "RANGE_LABEL",
    "IngressRouteTable",
    "routes_from_ingresses",
]


def routes_from_ingresses(
    ingresses: list[Mapping[str, object]],
) -> dict[tuple[UUID, str], Route]:
    """Build the table, skipping anything that does not describe exactly one backend.

    The labels are the whole contract. An earlier version fell back to reading the range out of
    the rule's host "for an Ingress written before the labels existed", which could never happen:
    the class name and both labels were introduced together, and the listing is filtered to the
    `pg-gateway` class before it gets here, so an Ingress old enough to lack the labels is on the
    `traefik` class and is never in the list. It is also still being served correctly by Traefik's
    middleware chain, which survives the upgrade -- adopting it here would double-serve it.
    """
    table: dict[tuple[UUID, str], Route] = {}
    for item in ingresses:
        metadata = item.get("metadata") or {}
        labels = metadata.get("labels") or {}
        namespace = metadata.get("namespace")
        app = labels.get(APP_LABEL)
        range_id = range_id_from_key(labels.get(RANGE_LABEL, ""))
        if not namespace or not app or range_id is None:
            logger.warning("ignoring ingress %s: it names no range and app", metadata.get("name"))
            continue

        backend = _single_backend(item)
        if backend is None:
            logger.warning(
                "ignoring ingress %s: expected exactly one backend", metadata.get("name")
            )
            continue
        service, port = backend
        table[(range_id, app)] = Route(namespace=namespace, service=service, port=port)
    return table


def _single_backend(item: Mapping[str, object]) -> Optional[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for rule in item.get("spec", {}).get("rules") or []:
        for path in (rule.get("http") or {}).get("paths") or []:
            service = ((path.get("backend") or {}).get("service")) or {}
            name, port = service.get("name"), (service.get("port") or {}).get("number")
            if name and isinstance(port, int):
                found.append((name, port))
    return found[0] if len(found) == 1 else None


class IngressRouteTable:
    """The table, refreshed from the cluster on a timer."""

    def __init__(self, kube, *, interval: float = 10.0) -> None:
        self._kube = kube
        self._interval = interval
        self._routes: dict[tuple[UUID, str], Route] = {}
        self._task: Optional[asyncio.Task] = None

    def lookup(self, range_id: UUID, app: str) -> Optional[Route]:
        return self._routes.get((range_id, app))

    async def refresh(self) -> None:
        """Replace the table. A failed read leaves the previous one in place.

        Serving the last known table through a blip is right: the alternative is every published
        application 404ing because the API server was briefly unreachable.
        """
        try:
            listing = await self._kube.list_ingresses_for_class(GATEWAY_INGRESS_CLASS)
        except Exception as exc:  # noqa: BLE001 - any read failure has the same answer
            logger.warning("route table not refreshed, serving the previous one: %s", exc)
            return
        self._routes = routes_from_ingresses(listing)

    async def run(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self._interval)

    async def start(self) -> None:
        await self.refresh()
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
