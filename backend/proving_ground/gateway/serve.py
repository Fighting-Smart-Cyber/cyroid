"""The process the pg-gateway Deployment runs.

Built from settings at import, so the chart configures it the same way it configures everything
else. What this pod is given, and what it is deliberately not given, is the whole argument: an
applications host, a path prefix, and a PUBLIC key. No database URL, no object-store keys, no
symmetric JWT secret -- it verifies range-application credentials and cannot mint anything.
"""

from __future__ import annotations

import logging

import httpx

from proving_ground.capability.kubernetes_client import KubernetesApiClient
from proving_ground.config import get_settings
from proving_ground.gateway.app import GatewayConfig, create_app
from proving_ground.gateway.table import IngressRouteTable

logger = logging.getLogger(__name__)


class _LazyKube:
    """Connect on first use, so an apiserver that is not ready yet is a retry, not a crash loop."""

    def __init__(self) -> None:
        self._client: KubernetesApiClient | None = None

    async def list_ingresses_for_class(self, ingress_class: str):
        if self._client is None:
            self._client = await KubernetesApiClient.in_cluster()
        return await self._client.list_ingresses_for_class(ingress_class)


def build() -> "object":
    settings = get_settings()
    config = GatewayConfig(
        apps_host=settings.range_apps_host,
        path_prefix=settings.range_ingress_path_prefix,
        secure_cookie=settings.console_cookie_secure,
    )
    table = IngressRouteTable(_LazyKube())
    # No connection pooling limits worth tuning yet; what matters is that redirects are NOT
    # followed -- a 302 from a training application belongs to the learner's browser, not to us.
    client = httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(30.0))
    return create_app(config, table, client)


app = build()
