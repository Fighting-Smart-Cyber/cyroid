"""C1(a): every teardown path must enqueue a pool refill after a successful
DinD container delete, and a refill-enqueue failure must never fail the
teardown itself.

Without this, refill_pool_task.send() has exactly one producer (a
successful claim), so an empty pool can never bootstrap - see
range_pool_service.py's module docstring and tasks/pool.py:24-31.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# api/ranges.py: delete_range (~line 387)
# ---------------------------------------------------------------------------


def _range_obj():
    obj = MagicMock()
    obj.id = uuid.uuid4()
    obj.dind_volume_name = "pg-range-abcd1234-docker"
    return obj


def test_delete_range_enqueues_refill_after_successful_teardown():
    from proving_ground.api import ranges as ranges_module

    range_id = uuid.uuid4()
    range_obj = _range_obj()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = range_obj

    fake_dind = MagicMock()
    fake_dind.delete_range_container = AsyncMock(return_value=None)
    fake_docker = MagicMock()

    with (
        patch.object(ranges_module, "get_dind_service", return_value=fake_dind),
        patch.object(ranges_module, "get_docker_service", return_value=fake_docker),
        patch("proving_ground.tasks.pool.refill_pool_task") as fake_task,
    ):
        ranges_module.delete_range(range_id=range_id, db=db, current_user=MagicMock())

    fake_task.send.assert_called_once()


def test_delete_range_succeeds_even_when_refill_enqueue_raises():
    from proving_ground.api import ranges as ranges_module

    range_id = uuid.uuid4()
    range_obj = _range_obj()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = range_obj

    fake_dind = MagicMock()
    fake_dind.delete_range_container = AsyncMock(return_value=None)
    fake_docker = MagicMock()

    with (
        patch.object(ranges_module, "get_dind_service", return_value=fake_dind),
        patch.object(ranges_module, "get_docker_service", return_value=fake_docker),
        patch("proving_ground.tasks.pool.refill_pool_task") as fake_task,
    ):
        fake_task.send.side_effect = RuntimeError("broker down")
        # Must not raise.
        ranges_module.delete_range(range_id=range_id, db=db, current_user=MagicMock())

    db.delete.assert_called_once_with(range_obj)
    db.commit.assert_called()


# ---------------------------------------------------------------------------
# api/ranges.py: teardown_range (~line 1352)
# ---------------------------------------------------------------------------


def test_teardown_range_enqueues_refill_after_successful_teardown():
    from proving_ground.api import ranges as ranges_module
    from proving_ground.models.range import RangeStatus

    range_id = uuid.uuid4()
    range_obj = _range_obj()
    range_obj.status = RangeStatus.RUNNING
    range_obj.dind_docker_url = "tcp://172.30.1.9:2375"
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = range_obj
    db.query.return_value.filter.return_value.all.return_value = []

    fake_dind = MagicMock()
    fake_dind.delete_range_container = AsyncMock(return_value=None)
    fake_docker = MagicMock()

    with (
        patch.object(ranges_module, "get_dind_service", return_value=fake_dind),
        patch.object(ranges_module, "get_docker_service", return_value=fake_docker),
        patch("proving_ground.tasks.pool.refill_pool_task") as fake_task,
    ):
        ranges_module.teardown_range(range_id=range_id, db=db, current_user=MagicMock())

    fake_task.send.assert_called_once()


def test_teardown_range_clears_dind_volume_name_too():
    """Minor (h): teardown cleared dind_container_id but not dind_volume_name."""
    from proving_ground.api import ranges as ranges_module
    from proving_ground.models.range import RangeStatus

    range_id = uuid.uuid4()
    range_obj = _range_obj()
    range_obj.status = RangeStatus.RUNNING
    range_obj.dind_docker_url = "tcp://172.30.1.9:2375"
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = range_obj
    db.query.return_value.filter.return_value.all.return_value = []

    fake_dind = MagicMock()
    fake_dind.delete_range_container = AsyncMock(return_value=None)
    fake_docker = MagicMock()

    with (
        patch.object(ranges_module, "get_dind_service", return_value=fake_dind),
        patch.object(ranges_module, "get_docker_service", return_value=fake_docker),
        patch("proving_ground.tasks.pool.refill_pool_task"),
    ):
        ranges_module.teardown_range(range_id=range_id, db=db, current_user=MagicMock())

    assert range_obj.dind_container_id is None
    assert range_obj.dind_volume_name is None


# ---------------------------------------------------------------------------
# api/training_events.py: _delete_event_ranges (~line 617)
# ---------------------------------------------------------------------------


def test_delete_event_ranges_enqueues_refill_after_successful_teardown():
    from proving_ground.api import training_events as te_module

    event_id = uuid.uuid4()
    range_id = uuid.uuid4()
    participant = MagicMock(range_id=range_id)
    range_obj = _range_obj()
    range_obj.id = range_id

    db = MagicMock()

    def query_side_effect(model):
        q = MagicMock()
        name = getattr(model, "__name__", "")
        if name == "EventParticipant":
            q.filter.return_value.all.return_value = [participant]
        elif name == "Range":
            q.filter.return_value.first.return_value = range_obj
        else:
            q.filter.return_value.all.return_value = []
            q.filter.return_value.delete.return_value = None
        return q

    db.query.side_effect = query_side_effect

    fake_dind = MagicMock()
    fake_dind.delete_range_container = AsyncMock(return_value=None)
    fake_docker = MagicMock()

    with (
        patch("proving_ground.services.dind_service.get_dind_service", return_value=fake_dind),
        patch(
            "proving_ground.services.docker_service.get_docker_service", return_value=fake_docker
        ),
        patch("proving_ground.tasks.pool.refill_pool_task") as fake_task,
    ):
        deleted = te_module._delete_event_ranges(event_id, db)

    assert deleted == 1
    fake_task.send.assert_called_once()


# ---------------------------------------------------------------------------
# api/admin.py: cleanup_all_resources (~line 142)
# ---------------------------------------------------------------------------


def test_admin_cleanup_enqueues_refill_after_successful_dind_delete():
    from proving_ground.api import admin as admin_module

    range_obj = _range_obj()
    range_obj.dind_container_id = "abcd1234"
    range_obj.dind_container_name = "pg-range-abcd1234"
    range_obj.status = MagicMock()

    db = MagicMock()
    db.query.return_value.all.return_value = [range_obj]

    fake_dind = MagicMock()
    fake_dind.delete_range_container = AsyncMock(return_value=None)
    fake_docker = MagicMock()
    fake_docker.cleanup_range.return_value = {"containers": 0, "networks": 0}

    with (
        patch.object(admin_module, "get_dind_service", return_value=fake_dind),
        patch.object(admin_module, "get_docker_service", return_value=fake_docker),
        patch("proving_ground.tasks.pool.refill_pool_task") as fake_task,
    ):
        admin_module.cleanup_all_resources(
            db=db, admin_user=MagicMock(email="a@b.com"), options=None
        )

    fake_task.send.assert_called_once()


# ---------------------------------------------------------------------------
# range_deployment_service.destroy_range
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_destroy_range_enqueues_refill_after_successful_delete():
    from proving_ground.services.range_deployment_service import RangeDeploymentService

    range_id = uuid.uuid4()
    range_obj = _range_obj()
    range_obj.id = range_id

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = range_obj
    db.query.return_value.filter.return_value.all.return_value = []

    svc = RangeDeploymentService.__new__(RangeDeploymentService)
    svc._docker_service = MagicMock()
    svc._dind_service = MagicMock()
    svc._dind_service.delete_range_container = AsyncMock(return_value=None)
    svc._pool_service = MagicMock()

    with (
        patch(
            "proving_ground.services.range_deployment_service.get_traefik_route_service"
        ) as fake_traefik,
        patch("proving_ground.tasks.pool.refill_pool_task") as fake_task,
    ):
        fake_traefik.return_value = MagicMock()
        await svc.destroy_range(db, range_id)

    fake_task.send.assert_called_once()


@pytest.mark.asyncio
async def test_destroy_range_clears_dind_volume_name_too():
    """Minor (h)."""
    from proving_ground.services.range_deployment_service import RangeDeploymentService

    range_id = uuid.uuid4()
    range_obj = _range_obj()
    range_obj.id = range_id

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = range_obj
    db.query.return_value.filter.return_value.all.return_value = []

    svc = RangeDeploymentService.__new__(RangeDeploymentService)
    svc._docker_service = MagicMock()
    svc._dind_service = MagicMock()
    svc._dind_service.delete_range_container = AsyncMock(return_value=None)
    svc._pool_service = MagicMock()

    with (
        patch(
            "proving_ground.services.range_deployment_service.get_traefik_route_service"
        ) as fake_traefik,
        patch("proving_ground.tasks.pool.refill_pool_task"),
    ):
        fake_traefik.return_value = MagicMock()
        await svc.destroy_range(db, range_id)

    assert range_obj.dind_container_id is None
    assert range_obj.dind_volume_name is None


@pytest.mark.asyncio
async def test_destroy_range_succeeds_even_when_refill_enqueue_raises():
    from proving_ground.services.range_deployment_service import RangeDeploymentService

    range_id = uuid.uuid4()
    range_obj = _range_obj()
    range_obj.id = range_id

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = range_obj
    db.query.return_value.filter.return_value.all.return_value = []

    svc = RangeDeploymentService.__new__(RangeDeploymentService)
    svc._docker_service = MagicMock()
    svc._dind_service = MagicMock()
    svc._dind_service.delete_range_container = AsyncMock(return_value=None)
    svc._pool_service = MagicMock()

    with (
        patch(
            "proving_ground.services.range_deployment_service.get_traefik_route_service"
        ) as fake_traefik,
        patch("proving_ground.tasks.pool.refill_pool_task") as fake_task,
    ):
        fake_traefik.return_value = MagicMock()
        fake_task.send.side_effect = RuntimeError("broker down")
        result = await svc.destroy_range(db, range_id)

    assert result["status"] == "destroyed"
