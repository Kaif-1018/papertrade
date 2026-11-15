"""Cache constants and reusable Redis primitives for distributed caching and locking.

Features:
- Cache key and TTL constants for the leaderboard
- Non-blocking lock acquisition with expiration (SET NX EX)
- Atomic lock release via Lua script (compare-and-delete)
- Safe cache retrieval, population, and invalidation with failure isolation
"""

import logging
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

# Cache key and TTL settings
LEADERBOARD_CACHE_KEY = "leaderboard:v1"
LEADERBOARD_LOCK_KEY = "leaderboard:lock"
LEADERBOARD_CACHE_TTL = 30  # seconds
LEADERBOARD_LOCK_TTL = 10   # seconds
LEADERBOARD_LOCK_WAIT_TIMEOUT = 2.0  # seconds
LEADERBOARD_LOCK_WAIT_POLL_INTERVAL = 0.05  # seconds

# Lua script for atomic compare-and-delete to ensure safe lock release:
# Only releases the lock if the key's value matches the calling worker's token.
_RELEASE_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


async def acquire_lock(
    redis: Redis,
    lock_key: str,
    token: str,
    expire_seconds: int = LEADERBOARD_LOCK_TTL,
) -> bool:
    """Attempt to acquire a distributed mutex lock using SET NX EX.

    Returns:
        True if lock was successfully acquired, False otherwise.
    """
    try:
        result = await redis.set(lock_key, token, nx=True, ex=expire_seconds)
        return bool(result)
    except RedisError as exc:
        logger.warning(
            "redis_acquire_lock_failed",
            extra={"lock_key": lock_key, "error": str(exc)},
        )
        return False


async def release_lock(redis: Redis, lock_key: str, token: str) -> bool:
    """Atomically release the lock only if the token matches.

    Prevents releasing a lock that has expired and been acquired by another worker.
    """
    try:
        result = await redis.eval(_RELEASE_LOCK_LUA, 1, lock_key, token)  # type: ignore[no-untyped-call]
        return bool(result)
    except RedisError as exc:
        logger.warning(
            "redis_release_lock_failed",
            extra={"lock_key": lock_key, "error": str(exc)},
        )
        return False


async def get_cached_leaderboard(redis: Redis) -> str | None:
    """Safely fetch cached leaderboard payload. Returns None on cache miss or Redis error."""
    try:
        val = await redis.get(LEADERBOARD_CACHE_KEY)
        return str(val) if val is not None else None
    except RedisError as exc:
        logger.warning("redis_get_leaderboard_cache_error", extra={"error": str(exc)})
        return None


async def set_cached_leaderboard(
    redis: Redis,
    payload: str,
    ttl: int = LEADERBOARD_CACHE_TTL,
) -> bool:
    """Safely write leaderboard payload to cache with TTL. Returns False on Redis error."""
    try:
        await redis.set(LEADERBOARD_CACHE_KEY, payload, ex=ttl)
        return True
    except RedisError as exc:
        logger.warning("redis_set_leaderboard_cache_error", extra={"error": str(exc)})
        return False


async def invalidate_leaderboard(redis: Redis | None = None) -> bool:
    """Invalidate cached leaderboard.

    Accepts an optional Redis client. If omitted, attempts to resolve via get_redis().
    Never propagates exceptions to callers.
    """
    try:
        client = redis if redis is not None else get_redis()
        deleted = await client.delete(LEADERBOARD_CACHE_KEY)
        return bool(deleted)
    except Exception as exc:
        logger.warning("leaderboard_cache_invalidation_failed", extra={"error": str(exc)})
        return False
