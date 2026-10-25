"""Async Redis client lifecycle management."""

from redis.asyncio import Redis

from app.core.config import get_settings

# Module-level reference; initialized in the app lifespan
redis_client: Redis | None = None


async def init_redis() -> Redis:
    """Create and store an async Redis connection."""
    global redis_client
    settings = get_settings()
    redis_client = Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
    )
    return redis_client


async def close_redis() -> None:
    """Gracefully close the Redis connection."""
    global redis_client
    if redis_client is not None:
        await redis_client.close()
        redis_client = None


def get_redis() -> Redis:
    """Return the active Redis client. Raises if not yet initialized."""
    if redis_client is None:
        raise RuntimeError("Redis client not initialized — is the app lifespan running?")
    return redis_client
