# backend/proving_ground/tasks/pool.py
"""Warm range pool maintenance tasks.

See range_pool_service.py's module docstring for the pool's security
invariant: a claimed container is never returned here. Refill always
provisions a fresh member via RangePoolService.provision_member().
"""
import asyncio
import logging
from typing import Optional

import dramatiq
import redis as redis_lib
from dramatiq.rate_limits import ConcurrentRateLimiter
from dramatiq.rate_limits.backends import RedisBackend

from proving_ground.config import get_settings
from proving_ground.services.range_pool_service import get_pool_service

logger = logging.getLogger(__name__)


# Distributed mutex serializing refill's read-shortfall-then-provision
# section (see refill_pool_task's docstring for why a bare "shortfall =
# target - pool_depth()" is not idempotent under concurrency:
# pool_depth() is a Redis SCARD over READY members only, so a member
# that's still mid-provision - booting its DinD container, then pulling a
# ~3.8 GB image, 40-60s - is invisible to it, and two refills racing each
# other both see the same shortfall and both provision the full amount).
#
# limit=1 makes this a plain distributed mutex - the exact pattern shown
# in dramatiq.rate_limits.RateLimiter's own docstring
# ("ConcurrentRateLimiter(backend, key, limit=1)" => ">>> Note: You can
# use a concurrent rate limiter of size 1 to get a distributed mutex").
#
# REFILL_MUTEX_TTL_MS is the failure-mode guard: if a worker crashes
# mid-refill (killed while pulling an image, OOM, etc.) the mutex would
# otherwise never be released, and every future refill - which fires
# unconditionally on every claim, every teardown AND every API startup -
# would silently see "mutex not acquired" and no-op forever, wedging the
# pool at whatever depth it happened to be at. The TTL is a self-healing
# floor: a crashed holder's slot is reclaimed automatically once it
# expires, comfortably above the worst case of provisioning a full
# range_pool_size sequentially (a handful of members at ~60s each).
REFILL_MUTEX_KEY = "pg:pool:refill-mutex"
REFILL_MUTEX_TTL_MS = 10 * 60 * 1000  # 10 minutes

_refill_mutex: Optional[ConcurrentRateLimiter] = None


def _get_refill_mutex() -> ConcurrentRateLimiter:
    """Lazily build the singleton refill mutex against the platform Redis.

    A module-level function (rather than inlining this in
    refill_pool_task) so tests can patch it to inject a StubBackend-based
    limiter instead of touching real Redis - see tests/unit/test_pool_refill.py.
    """
    global _refill_mutex
    if _refill_mutex is None:
        settings = get_settings()
        backend = RedisBackend(client=redis_lib.Redis.from_url(settings.redis_url))
        _refill_mutex = ConcurrentRateLimiter(
            backend, REFILL_MUTEX_KEY, limit=1, ttl=REFILL_MUTEX_TTL_MS
        )
    return _refill_mutex


def enqueue_pool_refill() -> None:
    """Enqueue a pool refill; never raises.

    Called after every successful pool claim, every successful range
    teardown (C1(a) - see the call sites in api/ranges.py,
    api/training_events.py, api/admin.py and
    range_deployment_service.destroy_range), and once at API startup
    (C1(b) - see main.py's lifespan). A broker hiccup here must never fail
    the deploy/teardown/startup that triggered it.
    """
    try:
        refill_pool_task.send()
    except Exception as e:
        logger.warning(f"Could not enqueue pool refill: {e}")


@dramatiq.actor(max_retries=2, min_backoff=30000, queue_name="pool")
def refill_pool_task(target: Optional[int] = None):
    """Top the warm pool up to its configured depth.

    Runs on the 'pool' queue, which shares a single worker (1 process, 6
    threads) with the 'deploys' queue (docker-compose.yml's worker-deploy
    service) rather than having a dedicated thread of its own - see I7 in
    the phase-3 fix-wave report. C1(a) moves the main refill trigger from
    claim time to teardown time, which is what actually keeps refill from
    competing with the deploy it just accelerated: a teardown's refill runs
    well before the next deploy's Stage 3 pulls start.

    IDEMPOTENT UNDER CONCURRENCY BY MUTEX, NOT BY SHORTFALL ALONE:
    "shortfall = target - pool_depth()" is NOT on its own safe to run from
    multiple concurrent refills, because pool_depth() (a Redis SCARD over
    the READY set) does not count members that are still mid-provision. A
    member takes 40-60s to boot its DinD container and pull its image
    before it is sadd'd into the ready set - invisible to pool_depth() the
    entire time. Two refills racing each other (e.g. three services
    restarting at once, each enqueuing a startup seed) would each read the
    same pre-provisioning depth and each provision the full shortfall,
    overshooting the target by a multiple of how many refills raced.

    What actually makes this task idempotent is _get_refill_mutex(): the
    whole read-shortfall-then-provision section below runs under a
    limit=1 distributed mutex (dramatiq.rate_limits.ConcurrentRateLimiter),
    so only one refill is ever inside that section at a time. acquire() is
    NON-BLOCKING here - a refill that finds the mutex already held (another
    refill is mid-provision) does not wait or retry, it logs and returns
    immediately, leaving the in-progress refill to finish the job. This
    task is enqueued unconditionally on every claim, teardown and startup,
    so a losing refill returning quietly (not raising) is the correct
    outcome, not a failure.
    """
    settings = get_settings()
    if target is None:
        target = settings.range_pool_size

    pool_service = get_pool_service()

    mutex = _get_refill_mutex()
    with mutex.acquire(raise_on_failure=False) as acquired:
        if not acquired:
            logger.info(
                "Pool refill already in progress on another worker/thread; skipping this run"
            )
            return

        ttl_hours = getattr(settings, "range_pool_member_ttl_hours", 0)
        try:
            reaped = pool_service.reap_expired_members(ttl_hours)
            if reaped:
                logger.info(f"TTL reap destroyed {reaped} expired pool member(s) before refill")
        except Exception as e:
            # Reaping is best-effort maintenance, never a reason to skip the
            # refill that follows.
            logger.warning(f"Pool TTL reap failed: {e}")

        # Before counting, re-adopt any member the ready set has forgotten.
        # Redis holds that set and nothing else does, so a Redis restart
        # leaves healthy members running and unclaimable; without this the
        # shortfall below is computed against a depth of 0 and the pool
        # provisions a second full set beside the orphans it just ignored.
        try:
            adopted = pool_service.reconcile_ready_set()
            if adopted:
                logger.info(f"Pool reconciliation re-adopted {adopted} orphaned member(s)")
        except Exception as e:
            logger.warning(f"Pool reconciliation failed: {e}")

        depth = pool_service.pool_depth()
        shortfall = max(0, target - depth)

        if shortfall == 0:
            logger.info(f"Pool already at target depth ({target}); refill is a no-op")
            return

        logger.info(f"Refilling pool: {shortfall} member(s) short of target {target}")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            for _ in range(shortfall):
                try:
                    loop.run_until_complete(pool_service.provision_member())
                except Exception as e:
                    # provision_member() is expected to be resilient on its own,
                    # but a refill batch must never let one bad member abort the
                    # rest of the shortfall.
                    logger.warning(f"Pool member provisioning failed during refill: {e}")
        finally:
            loop.close()
