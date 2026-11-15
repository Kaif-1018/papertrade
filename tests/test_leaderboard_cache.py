"""Integration tests for leaderboard Redis caching and stampede protection.

Verifies:
- Two consecutive requests issue only one computation (cache hit)
- Redis cache TTL is set (0 < ttl <= 30)
- Executing a trade commits and immediately invalidates the leaderboard cache
- Stampede protection: 20 concurrent requests on a cold cache result in exactly 1 computation
- Lock safety: releasing with mismatched token fails; expired lock does not block
- Graceful degradation: Redis failure or outage computes directly without raising 500
"""

import asyncio
from decimal import Decimal
import uuid

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.cache import (
    LEADERBOARD_CACHE_KEY,
    LEADERBOARD_LOCK_KEY,
    acquire_lock,
    invalidate_leaderboard,
    release_lock,
)
from app.repositories.leaderboard_repository import LeaderboardRepository
from app.services.leaderboard_service import LeaderboardService
from app.services.trade_service import TradeService


@pytest.mark.asyncio
async def test_leaderboard_cache_hit_and_ttl(
    clean_db, session_factory, create_user, test_redis: Redis
):
    """Two requests on populated cache execute DB computation exactly once and check TTL <= 30."""
    u1 = await create_user(balance="10000.00000000", email="user1@test.com")
    u2 = await create_user(balance="15000.00000000", email="user2@test.com")

    async with session_factory() as session:
        repo = LeaderboardRepository(session)
        service = LeaderboardService(repository=repo, redis=test_redis)

        # Instrument _compute_leaderboard_payload to count DB computations
        real_compute = service._compute_leaderboard_payload
        compute_count = 0

        async def counted_compute():
            nonlocal compute_count
            compute_count += 1
            return await real_compute()

        service._compute_leaderboard_payload = counted_compute  # type: ignore[method-assign]

        # Request 1: Cache miss -> computes
        res1 = await service.get_leaderboard(current_user_id=u1, limit=10)
        assert compute_count == 1
        assert len(res1.entries) == 2
        assert res1.entries[0].rank == 1

        # Check TTL in Redis
        ttl = await test_redis.ttl(LEADERBOARD_CACHE_KEY)
        assert 0 < ttl <= 30

        # Request 2: Cache hit -> reads from Redis without computing
        res2 = await service.get_leaderboard(current_user_id=u1, limit=10)
        assert compute_count == 1  # Unchanged!
        assert len(res2.entries) == 2
        assert res2.entries[0].total_value == res1.entries[0].total_value


@pytest.mark.asyncio
async def test_trade_invalidates_leaderboard_cache(
    clean_db, session_factory, create_user, set_price, test_redis: Redis
):
    """A committed trade immediately invalidates the leaderboard cache key."""
    u_id = await create_user(balance="10000.00000000")
    set_price("BTCUSDT", "50000.00000000")

    async with session_factory() as session:
        repo = LeaderboardRepository(session)
        service = LeaderboardService(repository=repo, redis=test_redis)

        # 1. Warm cache
        await service.get_leaderboard(current_user_id=u_id)
        assert await test_redis.exists(LEADERBOARD_CACHE_KEY) == 1

        # 2. Execute trade via TradeService
        trade_service = TradeService(session)
        await trade_service.buy(u_id, "BTCUSDT", Decimal("0.05000000"))

        # Wait briefly for post-commit fire-and-forget invalidation task to run
        await asyncio.sleep(0.05)

        # 3. Verify key is deleted from Redis
        assert await test_redis.exists(LEADERBOARD_CACHE_KEY) == 0


@pytest.mark.asyncio
async def test_stampede_protection_concurrent_requests(
    clean_db, session_factory, create_user, test_redis: Redis
):
    """20 concurrent requests on a cold cache acquire lock and result in exactly 1 computation."""
    u1 = await create_user(balance="10000.00000000", email="racer1@test.com")
    u2 = await create_user(balance="12000.00000000", email="racer2@test.com")

    # Clear cache before stampede test
    await test_redis.delete(LEADERBOARD_CACHE_KEY)

    compute_count = 0

    async def run_request(user_id):
        nonlocal compute_count
        async with session_factory() as session:
            repo = LeaderboardRepository(session)
            service = LeaderboardService(repository=repo, redis=test_redis)

            real_compute = service._compute_leaderboard_payload

            async def counted_compute():
                nonlocal compute_count
                compute_count += 1
                # Small artificial delay to ensure concurrency overlap
                await asyncio.sleep(0.05)
                return await real_compute()

            service._compute_leaderboard_payload = counted_compute  # type: ignore[method-assign]
            return await service.get_leaderboard(current_user_id=user_id, limit=20)

    # Launch 20 concurrent requests simultaneously
    tasks = [run_request(u1 if i % 2 == 0 else u2) for i in range(20)]
    results = await asyncio.gather(*tasks)

    # All 20 requests must succeed with valid data
    assert len(results) == 20
    for r in results:
        assert r.total_users == 2
        assert len(r.entries) == 2
        assert r.entries[0].rank == 1

    # Exactly one computation occurred!
    assert compute_count == 1


@pytest.mark.asyncio
async def test_lock_safety_and_expiry(test_redis: Redis):
    """Token matching prevents deleting another worker's lock; lock expires cleanly."""
    lock_key = "test:leaderboard:lock"
    await test_redis.delete(lock_key)

    token_a = "token-alpha"
    token_b = "token-beta"

    # 1. Acquire lock with token_a
    assert await acquire_lock(test_redis, lock_key, token_a, expire_seconds=2) is True

    # 2. Second acquire fails
    assert await acquire_lock(test_redis, lock_key, token_b, expire_seconds=2) is False

    # 3. Releasing with wrong token (token_b) fails and does not delete key
    assert await release_lock(test_redis, lock_key, token_b) is False
    assert await test_redis.get(lock_key) == token_a

    # 4. Releasing with correct token succeeds
    assert await release_lock(test_redis, lock_key, token_a) is True
    assert await test_redis.exists(lock_key) == 0

    # 5. Lock expiry: acquire with 1s TTL, wait 1.1s, verify key is free
    assert await acquire_lock(test_redis, lock_key, token_a, expire_seconds=1) is True
    await asyncio.sleep(1.1)
    # Next worker can acquire since it expired
    assert await acquire_lock(test_redis, lock_key, token_b, expire_seconds=2) is True
    await release_lock(test_redis, lock_key, token_b)


@pytest.mark.asyncio
async def test_redis_outage_graceful_degradation(clean_db, session_factory, create_user):
    """When Redis is down or raises errors, service degrades gracefully to direct DB compute."""
    u_id = await create_user(balance="10000.00000000", email="offline@test.com")

    # Case 1: Redis client is None
    async with session_factory() as session:
        repo = LeaderboardRepository(session)
        service_no_redis = LeaderboardService(repository=repo, redis=None)

        res1 = await service_no_redis.get_leaderboard(current_user_id=u_id)
        assert res1.total_users == 1
        assert res1.entries[0].display_name == "of***"
        assert res1.me is not None
        assert res1.me.rank == 1

    # Case 2: Redis operations raise RedisError
    class BrokenRedis:
        async def get(self, *args, **kwargs):
            raise RedisError("Simulated Redis connection refused")

        async def set(self, *args, **kwargs):
            raise RedisError("Simulated Redis connection refused")

        async def eval(self, *args, **kwargs):
            raise RedisError("Simulated Redis connection refused")

        async def delete(self, *args, **kwargs):
            raise RedisError("Simulated Redis connection refused")

    async with session_factory() as session:
        repo = LeaderboardRepository(session)
        service_broken_redis = LeaderboardService(
            repository=repo, redis=BrokenRedis()  # type: ignore[arg-type]
        )

        res2 = await service_broken_redis.get_leaderboard(current_user_id=u_id)
        assert res2.total_users == 1
        assert res2.entries[0].rank == 1
