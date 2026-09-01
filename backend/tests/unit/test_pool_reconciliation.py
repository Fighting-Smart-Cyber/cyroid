"""The warm pool must survive losing its Redis state.

Which members are claimable was recorded in exactly one place: the
POOL_READY_KEY set in Redis. Redis here runs with no AOF and its compose
service has no volume, so recreating that container -- which `compose up`
does whenever the service definition changes -- empties the set while every
member container keeps running.

Observed on pg-ec2: after a redeploy, two healthy pool members were still up
and holding their full memory reservation, `pg:pool:ready` was empty, and the
next Windows deploy provisioned a cold container beside them -- 52s instead of
the ~15s a warm claim gives. Nothing reported it.

The security half matters more than the speed half. A claimed member is a live
tenant's range, and re-adding one to the ready set would hand that running
container to whoever claims next.
"""

import inspect

import pytest

from proving_ground.services import range_pool_service as rps


class FakeContainer:
    def __init__(self, cid, name, labels, status="running"):
        self.id = cid
        self.name = name
        self.labels = labels
        self.status = status


class FakeRedis:
    def __init__(self, ready=()):
        self.ready = set(ready)

    def smembers(self, key):
        return set(self.ready)

    def sadd(self, key, value):
        self.ready.add(value)


class FakeContainers:
    def __init__(self, containers):
        self._containers = containers

    def list(self, filters=None):
        return list(self._containers)


class FakeDocker:
    def __init__(self, containers):
        self.containers = FakeContainers(containers)


class FakeDind:
    """Mirrors dind_service._get_container_name for the unclaimed case."""

    def _get_container_name(self, range_id, range_name=None):
        short = str(range_id).replace("-", "")[:8]
        return f"pg-range-{short}" if not range_name else f"pg-range-{range_name}-{short}"


POOL_ID = "6e49b05b-1111-2222-3333-444444444444"
UNCLAIMED_NAME = "pg-range-6e49b05b"


def _service(containers, ready=(), claimed=frozenset()):
    svc = rps.RangePoolService.__new__(rps.RangePoolService)
    svc.redis = FakeRedis(ready)
    svc.docker = FakeDocker(containers)
    svc.dind = FakeDind()
    svc._claimed_container_ids = lambda: set(claimed)
    return svc


def _member(cid="abc123def456", pool_id=POOL_ID, name=UNCLAIMED_NAME):
    return FakeContainer(
        cid, name, {"pg.type": "pool", rps.POOL_ID_LABEL: pool_id, "pg.pool_image_set": "[]"}
    )


class TestItAdoptsForgottenMembers:
    def test_a_running_member_absent_from_the_ready_set_is_adopted(self):
        svc = _service([_member()])
        assert svc.reconcile_ready_set() == 1
        assert "abc123def456" in svc.redis.ready

    def test_a_member_already_in_the_set_is_not_double_counted(self):
        svc = _service([_member()], ready={"abc123def456"})
        assert svc.reconcile_ready_set() == 0

    def test_nothing_to_do_is_not_an_error(self):
        assert _service([]).reconcile_ready_set() == 0


class TestItNeverAdoptsALiveRange:
    """Both guards are load-bearing; each is tested with the other satisfied."""

    def test_a_renamed_container_is_never_adopted(self):
        """claim() renames as its final step, so a renamed member is spoken for."""
        svc = _service([_member(name="pg-range-windows-gold-test-cc058e29")])
        assert svc.reconcile_ready_set() == 0
        assert not svc.redis.ready

    def test_a_container_referenced_by_a_range_is_never_adopted(self):
        svc = _service([_member()], claimed={"abc123def456"})
        assert svc.reconcile_ready_set() == 0
        assert not svc.redis.ready

    def test_an_unreadable_range_table_adopts_nothing(self):
        """Guessing here would hand a tenant's container to another tenant."""
        svc = _service([_member()])
        svc._claimed_container_ids = lambda: None
        assert svc.reconcile_ready_set() == 0
        assert not svc.redis.ready

    def test_a_container_without_a_pool_id_label_is_ignored(self):
        svc = _service([FakeContainer("abc123def456", UNCLAIMED_NAME, {"pg.type": "pool"})])
        assert svc.reconcile_ready_set() == 0


class TestItIsBestEffort:
    def test_a_docker_failure_returns_zero_rather_than_raising(self):
        svc = _service([])

        def boom(filters=None):
            raise RuntimeError("docker is down")

        svc.docker.containers.list = boom
        assert svc.reconcile_ready_set() == 0

    def test_only_running_containers_are_considered(self):
        """Asserted on the filter, since the fake cannot enforce it itself."""
        src = inspect.getsource(rps.RangePoolService.reconcile_ready_set)
        assert '"status": "running"' in src


class TestTheRefillReconcilesBeforeCounting:
    """Asserted by calling it, not by reading the source: the first version of
    this test compared source offsets and failed on the docstring, which
    mentions pool_depth() sixty lines before the call does."""

    def test_reconcile_runs_before_the_depth_is_read(self, monkeypatch):
        """Otherwise the shortfall is computed against a depth of 0 and the pool
        provisions a second full set beside the orphans it just ignored."""
        from contextlib import contextmanager

        from proving_ground.tasks import pool as pool_task

        calls = []

        class StubPool:
            def reconcile_ready_set(self):
                calls.append("reconcile")
                return 0

            def reap_expired_members(self, ttl_hours):
                return 0

            def pool_depth(self):
                calls.append("depth")
                return 99  # already above target, so nothing is provisioned

        class StubMutex:
            @contextmanager
            def acquire(self, raise_on_failure=False):
                yield True

        monkeypatch.setattr(pool_task, "get_pool_service", lambda: StubPool())
        monkeypatch.setattr(pool_task, "_get_refill_mutex", lambda: StubMutex())

        pool_task.refill_pool_task.fn(target=1)

        assert calls == ["reconcile", "depth"], (
            f"call order was {calls}; reconciliation must run before the depth "
            "is read, or orphaned members are not counted."
        )


@pytest.mark.parametrize("attr", ["reconcile_ready_set", "_claimed_container_ids"])
def test_the_methods_exist_on_the_service(attr):
    assert hasattr(rps.RangePoolService, attr)
