"""Refill tops the pool up to its configured depth and never exceeds it,
even when multiple refills race each other (idempotency-under-concurrency
fix, phase-3 warm-range-pool bring-up)."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def fake_refill_mutex():
    """Every test gets its own StubBackend-based mutex, patched in for the
    real Redis-backed one. Without this, refill_pool_task would build a
    ConcurrentRateLimiter against the REAL platform Redis (tasks/pool.py's
    _get_refill_mutex() uses settings.redis_url) - unit tests must never
    touch the live broker/Redis instance. Tests that care about a specific
    mutex state (already held, shared across "concurrent" calls) either
    reuse this fixture's mutex directly or override the patch locally.
    """
    from dramatiq.rate_limits import ConcurrentRateLimiter
    from dramatiq.rate_limits.backends import StubBackend
    import proving_ground.tasks.pool as pool_module

    backend = StubBackend()
    mutex = ConcurrentRateLimiter(backend, "test-refill-mutex", limit=1, ttl=60_000)
    with patch.object(pool_module, "_get_refill_mutex", return_value=mutex):
        yield mutex


def test_refill_provisions_up_to_configured_depth():
    with patch("proving_ground.tasks.pool.get_pool_service") as gp:
        svc = MagicMock()
        svc.pool_depth.return_value = 0
        gp.return_value = svc

        from proving_ground.tasks.pool import refill_pool_task

        refill_pool_task(target=2)

        assert svc.provision_member.call_count == 2


def test_refill_is_a_noop_when_pool_is_full():
    with patch("proving_ground.tasks.pool.get_pool_service") as gp:
        svc = MagicMock()
        svc.pool_depth.return_value = 2
        gp.return_value = svc

        from proving_ground.tasks.pool import refill_pool_task

        refill_pool_task(target=2)

        svc.provision_member.assert_not_called()


def test_refill_runs_on_its_own_queue():
    """Refill must never compete with a user waiting on a deploy."""
    from proving_ground.tasks.pool import refill_pool_task

    assert refill_pool_task.queue_name == "pool"


# ---------------------------------------------------------------------------
# Concurrency: pool_depth() (a Redis SCARD over the READY set) is blind to a
# member that is still mid-provision - booting its DinD container, then
# pulling its image, 40-60s. Two refills racing each other must not each
# provision the full shortfall. See tasks/pool.py's module-level comment
# above _get_refill_mutex and refill_pool_task's own docstring.
# ---------------------------------------------------------------------------


def test_two_concurrent_refills_provision_two_not_four():
    """Simulates concurrency directly (no threads): the mocked
    provision_member, on its first invocation, itself calls
    refill_pool_task() again before returning - modeling a second refill
    that starts while the first is still mid-provision and holding the
    mutex. pool_depth stays 0 throughout for both, mirroring reality (a
    mid-provision member never reaches the ready set, so a naive shortfall
    computed by either refill would independently be 2). Total
    provisioning across both refills must be 2, not 4.
    """
    import proving_ground.tasks.pool as pool_module

    with patch.object(pool_module, "get_pool_service") as gp:
        svc = MagicMock()
        svc.pool_depth.return_value = 0
        gp.return_value = svc

        calls = []

        def provision_side_effect():
            calls.append(1)
            if len(calls) == 1:
                # A second, "concurrent" refill fires while the first
                # still holds the mutex - it must see "not acquired" and
                # no-op rather than also computing/acting on a shortfall.
                pool_module.refill_pool_task(target=2)

        svc.provision_member.side_effect = provision_side_effect

        pool_module.refill_pool_task(target=2)

    assert svc.provision_member.call_count == 2


def test_refill_returns_quietly_and_provisions_nothing_when_mutex_held():
    """A refill that finds the mutex already held (another refill is
    mid-provision elsewhere) must return without raising and without
    touching the pool at all - not even the shortfall read."""
    import proving_ground.tasks.pool as pool_module
    from dramatiq.rate_limits import ConcurrentRateLimiter
    from dramatiq.rate_limits.backends import StubBackend

    backend = StubBackend()
    mutex = ConcurrentRateLimiter(backend, "held-elsewhere", limit=1, ttl=60_000)
    held = mutex.acquire(raise_on_failure=False)
    assert held.__enter__() is True  # simulate another in-flight refill

    try:
        with (
            patch.object(pool_module, "_get_refill_mutex", return_value=mutex),
            patch.object(pool_module, "get_pool_service") as gp,
        ):
            svc = MagicMock()
            svc.pool_depth.return_value = 0
            gp.return_value = svc

            pool_module.refill_pool_task(target=2)  # must not raise

            svc.pool_depth.assert_not_called()
            svc.reap_expired_members.assert_not_called()
            svc.provision_member.assert_not_called()
    finally:
        held.__exit__(None, None, None)


def test_mutex_is_released_even_when_the_protected_section_raises():
    """provision_member()'s own failures never reach the mutex boundary -
    they're swallowed per-member by design (refill_pool_task's inner
    try/except: "a refill batch must never let one bad member abort the
    rest of the shortfall"). What CAN escape the protected section is
    anything else in it, e.g. pool_depth() itself - and if that happened
    while leaving the mutex held, every subsequent refill (enqueued
    unconditionally on every claim/teardown/startup) would silently no-op
    forever, wedging the pool. This proves that doesn't happen: a raise
    from inside the mutex still releases it, verified by a second,
    ordinary refill succeeding immediately afterward.
    """
    import proving_ground.tasks.pool as pool_module
    from dramatiq.rate_limits import ConcurrentRateLimiter
    from dramatiq.rate_limits.backends import StubBackend

    backend = StubBackend()
    mutex = ConcurrentRateLimiter(backend, "raises-inside", limit=1, ttl=60_000)

    with (
        patch.object(pool_module, "_get_refill_mutex", return_value=mutex),
        patch.object(pool_module, "get_pool_service") as gp,
    ):
        svc = MagicMock()
        svc.pool_depth.side_effect = RuntimeError("redis blew up")
        gp.return_value = svc

        with pytest.raises(RuntimeError):
            pool_module.refill_pool_task(target=2)

        # Right after the first call raised: if the mutex had leaked, this
        # would silently no-op ("mutex not acquired") instead of actually
        # provisioning.
        svc.pool_depth.side_effect = None
        svc.pool_depth.return_value = 0
        pool_module.refill_pool_task(target=2)

        assert svc.provision_member.call_count == 2
