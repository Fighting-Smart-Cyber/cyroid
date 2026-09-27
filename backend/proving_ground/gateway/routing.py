"""Which range, which application, and where that application actually listens.

Both questions are answered from the request itself and both fail closed: a request that does
not name exactly one range, and one application published for it, gets nothing. There is no
default range and no fallback application.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol
from uuid import UUID

from proving_ground.utils.range_hosts import range_id_from_host

__all__ = ["Route", "RouteTable", "range_id_from_host", "split_app_path"]


@dataclass(frozen=True, slots=True)
class Route:
    """Where a request goes once it has been authorised."""

    namespace: str
    service: str
    port: int

    @property
    def host(self) -> str:
        """The in-cluster name. Fully qualified so it does not depend on search domains."""
        return f"{self.service}.{self.namespace}.svc.cluster.local"


class RouteTable(Protocol):
    """What the gateway knows about published applications.

    A Protocol because the source is a separate decision from the proxying: the gateway will
    watch Ingress objects carrying its own IngressClass, and a dict is what the tests use.
    """

    def lookup(self, range_id: UUID, app: str) -> Optional[Route]: ...


def split_app_path(path: str, prefix: str) -> Optional[tuple[str, str]]:
    """(app, the path the application should see), or None if the path names no application.

    An application is published at `<prefix>/<app>/...` and must be served `/...`, because it
    was never told it lives under a prefix. This is what the StripPrefix middleware did, and
    doing it here is why that CRD is no longer needed.
    """
    prefix = prefix.rstrip("/")
    if prefix and not path.startswith(prefix + "/"):
        return None
    rest = path[len(prefix) :] if prefix else path
    if not rest.startswith("/"):
        return None
    app, _, tail = rest[1:].partition("/")
    if not app:
        return None
    return app, "/" + tail
