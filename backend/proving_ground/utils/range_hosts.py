"""Which range a hostname names.

One definition, because there were two halves of it in two places: the canonical-UUID rule in
`kubernetes_range_service.range_id_from_key`, and the suffix-and-port handling inside
`kubernetes_apps._host_and_range`. pg-gateway needs exactly the same answer, and a second
implementation of a security-relevant parser is how the two drift.

Nothing here imports anything from the engine, so the gateway can use it without dragging in a
Kubernetes client or the ORM.
"""

from __future__ import annotations

import re
from typing import Optional
from uuid import UUID

# A port is `:` and digits and nothing else. Anything else stays in the string the suffix is
# matched against, and therefore fails -- which is the intent: a weird authority is not a range.
_PORT = re.compile(r":\d+$")


def range_id_from_key(key: str) -> Optional[UUID]:
    """The range a DNS label names, or None.

    Only the canonical spelling is accepted, not the braced, URN or unhyphenated forms `UUID()`
    will also swallow: the ticket cookie is host-scoped, so a second accepted spelling is a
    second security context for the same range rather than a convenience.
    """
    try:
        parsed = UUID(key)
    except (AttributeError, ValueError):
        return None
    return parsed if str(parsed) == key else None


def range_id_from_host(authority: str, apps_host: str) -> Optional[UUID]:
    """The range an authority (`host` or `host:port`) names, or None.

    Exactly one label beneath the configured suffix. Without that rule `a.b.<suffix>` resolves,
    and a range would answer on names nobody published.
    """
    if not authority or not apps_host:
        return None
    suffix = apps_host.strip().lower().strip(".")
    host = _PORT.sub("", authority.strip().lower()).rstrip(".")
    if not suffix or not host.endswith(f".{suffix}"):
        return None
    label = host[: -len(suffix) - 1]
    if not label or "." in label:
        return None
    return range_id_from_key(label)
