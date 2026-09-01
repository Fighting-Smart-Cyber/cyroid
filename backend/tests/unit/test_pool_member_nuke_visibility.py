"""I3: the admin 'remove ALL resources' nuke must see warm-claimed ranges.

docker_service.cleanup_all_proving_ground_resources() previously matched
containers only by pg.range_id / pg.vm_id. A pool member claimed for a range
is renamed to pg-range-* by range_pool_service.claim(), but Docker labels
are immutable on a running container, so it keeps pg.type=pool and NEVER
gains pg.range_id. DB-tracked ranges still clean up fine (dind_service's
_find_container_by_range_id has a name-suffix fallback), but an orphaned
warm range - claimed, then abandoned before its DB row/label ever pointed
back at it, or a DB row that was separately deleted - was permanently
invisible to this nuke: the trainee-root container stayed running while the
API reported success.

The fix matches pg.type=pool containers named pg-range-* too, but a FRESH,
never-claimed pool member is ALSO named pg-range-{short_id} (see
dind_service._get_container_name), so name+label alone can't tell an orphan
apart from a perfectly healthy member still sitting in the warm pool. The
fix cross-checks the pool's own bookkeeping (pg:pool:ready, the Redis set
claim() spop()s from): a container id still in that set is left alone
unless the caller explicitly opts into a full teardown via
include_ready_pool_members=True.
"""

import uuid

from unittest.mock import AsyncMock, MagicMock, patch


def _container(name, container_id, labels):
    c = MagicMock()
    c.name = name
    c.id = container_id
    c.labels = labels
    return c


def _service_with(containers, ready_member_ids=()):
    from proving_ground.services.docker_service import DockerService

    svc = DockerService.__new__(DockerService)
    svc.client = MagicMock()
    svc.client.containers.list.return_value = containers
    svc.client.networks.list.return_value = []

    fake_pool_service = MagicMock()
    fake_pool_service.redis.smembers.return_value = set(ready_member_ids)

    patcher = patch(
        "proving_ground.services.range_pool_service.get_pool_service",
        return_value=fake_pool_service,
    )
    return svc, patcher


def test_cleanup_removes_a_claimed_and_orphaned_pool_member():
    """pg.type=pool, pg-range- named, NOT in the ready set: this is exactly
    a claimed member whose range never (or no longer) tracks it - the I3
    gap. It must be destroyed, not silently skipped."""
    orphan = _container("pg-range-abcd1234", "orphan-id", {"pg.type": "pool"})
    svc, patcher = _service_with([orphan], ready_member_ids=[])

    with patcher:
        results = svc.cleanup_all_proving_ground_resources()

    orphan.remove.assert_called_once_with(force=True)
    assert results["containers_removed"] == 1


def test_cleanup_spares_a_healthy_ready_pool_member_by_default():
    """pg.type=pool, pg-range- named, but STILL in pg:pool:ready: this is a
    perfectly healthy, unclaimed warm member, not an orphan. The nuke must
    not destroy it unless the caller explicitly asked for a full teardown."""
    healthy = _container("pg-range-ef012345", "healthy-id", {"pg.type": "pool"})
    svc, patcher = _service_with([healthy], ready_member_ids=["healthy-id"])

    with patcher:
        results = svc.cleanup_all_proving_ground_resources()

    healthy.remove.assert_not_called()
    assert results["containers_removed"] == 0


def test_cleanup_include_ready_pool_members_removes_healthy_members_too():
    """The explicit opt-in for a full teardown must still reach a healthy
    ready pool member."""
    healthy = _container("pg-range-ef012345", "healthy-id", {"pg.type": "pool"})
    svc, patcher = _service_with([healthy], ready_member_ids=["healthy-id"])

    with patcher:
        results = svc.cleanup_all_proving_ground_resources(include_ready_pool_members=True)

    healthy.remove.assert_called_once_with(force=True)
    assert results["containers_removed"] == 1


def test_cleanup_still_removes_db_tracked_range_containers():
    """Existing behavior unchanged: a normal, DB-tracked range container
    (pg.range_id label) is removed regardless of the pool ready set."""
    tracked = _container("pg-range-tracked-abcd1234", "tracked-id", {"pg.range_id": "r-1"})
    svc, patcher = _service_with([tracked], ready_member_ids=[])

    with patcher:
        results = svc.cleanup_all_proving_ground_resources()

    tracked.remove.assert_called_once_with(force=True)
    assert results["containers_removed"] == 1


def test_cleanup_ignores_a_pool_container_not_shaped_like_a_range():
    """A pool member whose name does not match the pg-range- prefix (should
    not occur in practice - provision_member() always names via
    _get_container_name - but guards the matcher against being loosened to
    'any pg.type=pool container' by accident) is left alone."""
    weird = _container("pg-something-else", "weird-id", {"pg.type": "pool"})
    svc, patcher = _service_with([weird], ready_member_ids=[])

    with patcher:
        results = svc.cleanup_all_proving_ground_resources()

    weird.remove.assert_not_called()
    assert results["containers_removed"] == 0


# ---------------------------------------------------------------------------
# Wiring: the admin API's CleanupRequest.force must reach
# include_ready_pool_members. A previous fix wave added the parameter above
# but deliberately left it unwired to avoid silently changing endpoint
# behaviour - this closes that gap explicitly, with the default (force=False)
# proven NOT to destroy healthy pool members.
# ---------------------------------------------------------------------------


def _admin_cleanup_env():
    """Build a minimal environment for calling admin_module.cleanup_all_resources
    with zero DB-tracked ranges, so only the final orphan-sweep step (which
    calls docker.cleanup_all_proving_ground_resources) matters."""
    db = MagicMock()
    db.query.return_value.all.return_value = []

    fake_dind = MagicMock()
    fake_dind.list_range_containers = AsyncMock(return_value=[])

    fake_docker = MagicMock()
    fake_docker.cleanup_all_proving_ground_resources.return_value = {
        "containers_removed": 0,
        "networks_removed": 0,
        "errors": [],
    }
    return db, fake_dind, fake_docker


def test_admin_cleanup_default_does_not_drain_ready_pool_members():
    """force defaults to False - the nuke must not opt into draining healthy
    ready pool members unless the operator explicitly asks."""
    from proving_ground.api import admin as admin_module

    db, fake_dind, fake_docker = _admin_cleanup_env()

    with (
        patch.object(admin_module, "get_dind_service", return_value=fake_dind),
        patch.object(admin_module, "get_docker_service", return_value=fake_docker),
    ):
        admin_module.cleanup_all_resources(
            db=db, admin_user=MagicMock(email="a@b.com"), options=None
        )

    fake_docker.cleanup_all_proving_ground_resources.assert_called_once_with(
        include_ready_pool_members=False
    )


def test_admin_cleanup_force_true_drains_ready_pool_members():
    """force=True is the operator's explicit opt-in to a full teardown that
    also empties the warm pool."""
    from proving_ground.api import admin as admin_module

    db, fake_dind, fake_docker = _admin_cleanup_env()

    with (
        patch.object(admin_module, "get_dind_service", return_value=fake_dind),
        patch.object(admin_module, "get_docker_service", return_value=fake_docker),
    ):
        admin_module.cleanup_all_resources(
            db=db,
            admin_user=MagicMock(email="a@b.com"),
            options=admin_module.CleanupRequest(force=True),
        )

    fake_docker.cleanup_all_proving_ground_resources.assert_called_once_with(
        include_ready_pool_members=True
    )
