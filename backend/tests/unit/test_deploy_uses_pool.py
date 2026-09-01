"""Deploy prefers a pooled container and falls back cleanly when there is none."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _svc(claim_returns):
    from proving_ground.services.range_deployment_service import RangeDeploymentService

    s = RangeDeploymentService.__new__(RangeDeploymentService)
    s.pool_service = MagicMock()
    s.pool_service.claim.return_value = claim_returns
    s.dind_service = MagicMock()
    s.dind_service.create_range_container = AsyncMock(
        return_value={"container_id": "cold", "volume_name": "pg-range-cold-docker"}
    )
    return s


WARM = {"container_id": "warm", "volume_name": "pg-range-pool-docker"}


@pytest.mark.asyncio
async def test_uses_the_pool_when_a_member_is_available():
    s = _svc(WARM)
    got = await s._acquire_dind("r1", "One", ["img"], "8g", 4.0, None)

    assert got["container_id"] == "warm"
    s.dind_service.create_range_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_falls_back_to_cold_when_claim_returns_none():
    """An empty pool must still produce a working deploy."""
    s = _svc(None)
    got = await s._acquire_dind("r1", "One", ["img"], "8g", 4.0, None)

    assert got["container_id"] == "cold"
    s.dind_service.create_range_container.assert_awaited_once()


@pytest.mark.asyncio
async def test_pool_disabled_skips_the_claim_entirely(monkeypatch):
    from proving_ground.config import get_settings

    monkeypatch.setattr(get_settings(), "range_pool_enabled", False, raising=False)

    s = _svc(WARM)
    got = await s._acquire_dind("r1", "One", ["img"], "8g", 4.0, None)

    assert got["container_id"] == "cold"
    s.pool_service.claim.assert_not_called()


# =====================================================================
# I5: nothing guarded RangePoolService construction. self.pool_service is
# a property that lazily calls get_pool_service() -> RangePoolService() ->
# docker.from_env(), which on docker-py 7.1.0 calls
# _retrieve_server_version() and raises DockerException when the socket is
# unreachable. That construction happened OUTSIDE claim()'s own try/except
# (claim() only guards itself, not the property access that resolves
# self.pool_service in the first place), so a pool-side Docker problem
# failed the whole deploy instead of falling through to cold provisioning.
# =====================================================================


@pytest.mark.asyncio
async def test_acquire_dind_cold_provisions_when_get_pool_service_raises():
    """The degradation property the Redis-down test could never cover,
    because that test goes through a pool_service that already
    constructed successfully."""
    from proving_ground.services.range_deployment_service import RangeDeploymentService
    import docker.errors

    s = RangeDeploymentService.__new__(RangeDeploymentService)
    s._pool_service = None  # force the property to call get_pool_service()
    s.dind_service = MagicMock()
    s.dind_service.create_range_container = AsyncMock(
        return_value={"container_id": "cold", "volume_name": "pg-range-cold-docker"}
    )

    with patch(
        "proving_ground.services.range_deployment_service.get_pool_service",
        side_effect=docker.errors.DockerException("Docker socket unreachable"),
    ):
        got = await s._acquire_dind("r1", "One", ["img"], "8g", 4.0, None)

    assert got["container_id"] == "cold"
    s.dind_service.create_range_container.assert_awaited_once()
