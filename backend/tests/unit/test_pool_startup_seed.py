"""C1(b): the API must seed the warm pool once at startup.

Without this, refill_pool_task.send() is only ever enqueued after a
successful claim or (as of C1(a)) a teardown - so a fresh deploy with an
empty pool and no prior range activity runs cold forever. See
range_pool_service.py's module docstring and the fixwave brief's C1.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _patched_realtime_services():
    fake_connection_manager = MagicMock()
    fake_connection_manager.start = AsyncMock(return_value=None)
    fake_connection_manager.stop = AsyncMock(return_value=None)
    fake_broadcaster = MagicMock()
    fake_broadcaster.connect = AsyncMock(return_value=None)
    fake_broadcaster.disconnect = AsyncMock(return_value=None)
    return fake_connection_manager, fake_broadcaster


@pytest.mark.asyncio
async def test_lifespan_enqueues_pool_refill_on_startup_when_pool_enabled(monkeypatch):
    from proving_ground import main as main_module

    monkeypatch.setattr(main_module.settings, "range_pool_enabled", True, raising=False)
    fake_connection_manager, fake_broadcaster = _patched_realtime_services()

    with (
        patch(
            "proving_ground.services.event_broadcaster.get_connection_manager",
            return_value=fake_connection_manager,
        ),
        patch(
            "proving_ground.services.event_broadcaster.get_broadcaster",
            return_value=fake_broadcaster,
        ),
        patch("proving_ground.tasks.pool.refill_pool_task") as fake_task,
    ):
        async with main_module.lifespan(main_module.app):
            pass

    fake_task.send.assert_called_once()


@pytest.mark.asyncio
async def test_lifespan_skips_seeding_when_pool_disabled(monkeypatch):
    from proving_ground import main as main_module

    monkeypatch.setattr(main_module.settings, "range_pool_enabled", False, raising=False)
    fake_connection_manager, fake_broadcaster = _patched_realtime_services()

    with (
        patch(
            "proving_ground.services.event_broadcaster.get_connection_manager",
            return_value=fake_connection_manager,
        ),
        patch(
            "proving_ground.services.event_broadcaster.get_broadcaster",
            return_value=fake_broadcaster,
        ),
        patch("proving_ground.tasks.pool.refill_pool_task") as fake_task,
    ):
        async with main_module.lifespan(main_module.app):
            pass

    fake_task.send.assert_not_called()


@pytest.mark.asyncio
async def test_lifespan_startup_survives_refill_enqueue_failure(monkeypatch):
    """A broker hiccup must not stop the API from booting."""
    from proving_ground import main as main_module

    monkeypatch.setattr(main_module.settings, "range_pool_enabled", True, raising=False)
    fake_connection_manager, fake_broadcaster = _patched_realtime_services()

    with (
        patch(
            "proving_ground.services.event_broadcaster.get_connection_manager",
            return_value=fake_connection_manager,
        ),
        patch(
            "proving_ground.services.event_broadcaster.get_broadcaster",
            return_value=fake_broadcaster,
        ),
        patch("proving_ground.tasks.pool.refill_pool_task") as fake_task,
    ):
        fake_task.send.side_effect = RuntimeError("broker down")
        # Must not raise.
        async with main_module.lifespan(main_module.app):
            pass
