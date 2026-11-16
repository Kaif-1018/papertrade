"""Redis-backed sliding-window rate limiter with atomic Lua script execution.

Architecture & Trade-offs:
- Sliding Window via Redis Sorted Sets (ZSET):
  Ensures precision without window boundary bursting (unlike fixed-window counters).
  An atomic Lua script performs pruning of expired timestamps, card counting,
  and insertion in a single round-trip, preventing race conditions under high concurrency.
- Security vs. Availability Fail-Open / Fail-Closed Trade-off:
  - Authentication endpoints (login, register): Fail CLOSED when Redis is unreachable.
    Authentication endpoints are prime targets for automated credential stuffing and
    brute-force dictionary attacks. Failing open during a cache outage removes protection
    entirely, creating an exploitable window where attackers could flood credentials.
    We prioritize user security over availability for auth routes.
  - General API endpoints: Fail OPEN when Redis is unreachable.
    Read-only or business endpoints are either already authenticated or non-credential-bearing;
    prioritizing platform availability during partial cache outages provides a better user
    experience without compromising cryptographic credential security.
- Reverse Proxy Security:
  X-Forwarded-For is only trusted when settings.TRUST_PROXY is True. When running directly
  or behind untrusted proxies, the socket client IP is used to prevent header spoofing.
"""

from collections.abc import Callable
import math
import time
from typing import Literal

from fastapi import Request, Response, WebSocket
import jwt
import structlog

from app.core.config import get_settings
from app.core.exceptions import RateLimitExceeded
from app.core.redis import get_redis

logger = structlog.stdlib.get_logger(__name__)

SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local clear_before = now - window

-- Prune requests older than current sliding window
redis.call('ZREMRANGEBYSCORE', key, '-inf', clear_before)

-- Count requests currently inside window
local current_count = redis.call('ZCARD', key)

if current_count < limit then
    -- Unique member per request prevents timestamp collisions
    local seq = redis.call('INCR', key .. ':seq')
    local member = tostring(now) .. ':' .. tostring(seq)
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, math.ceil(window) + 2)
    redis.call('EXPIRE', key .. ':seq', math.ceil(window) + 2)
    return {1, limit - current_count - 1, math.ceil(now + window)}
else
    -- Retrieve oldest timestamp in window to compute exact reset time
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local reset_time = math.ceil(now + window)
    if #oldest >= 2 then
        reset_time = math.ceil(tonumber(oldest[2]) + window)
    end
    return {0, 0, reset_time}
end
"""


def get_client_ip(request: Request | WebSocket, trust_proxy: bool) -> str:
    """Extract client IP, strictly obeying TRUST_PROXY settings to prevent header spoofing."""
    if trust_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def get_auth_sub(request: Request) -> str | None:
    """Extract subject (user_id) from Authorization header without full DB lookup."""
    auth_header = request.headers.get("authorization")
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header[7:].strip()
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        return payload.get("sub")
    except Exception:
        return None


def rate_limit(
    name: str,
    limit: int,
    window_seconds: int,
    key: Literal["ip", "user"] = "ip",
    fail_closed: bool = False,
) -> Callable:
    """Dependency factory returning an async rate-limiting validator."""

    async def _rate_limiter(request: Request, response: Response) -> None:
        settings = get_settings()
        trust_proxy = settings.TRUST_PROXY

        # Determine rate limit key
        ip = get_client_ip(request, trust_proxy)
        if key == "user":
            sub = get_auth_sub(request)
            identifier = f"user:{sub}" if sub else f"ip:{ip}"
        else:
            identifier = f"ip:{ip}"

        redis_key = f"rate_limit:{name}:{identifier}"
        now = time.time()

        try:
            # Respect FastAPI dependency overrides for testability
            redis_override = request.app.dependency_overrides.get(get_redis)
            if redis_override is not None:
                redis_client = redis_override() if callable(redis_override) else redis_override
            else:
                try:
                    redis_client = get_redis()
                except RuntimeError:
                    from app.core.redis import init_redis
                    redis_client = await init_redis()

            res = await redis_client.eval(
                SLIDING_WINDOW_LUA,
                1,
                redis_key,
                str(now),
                str(window_seconds),
                str(limit),
            )
            allowed, remaining, reset_time = int(res[0]), int(res[1]), int(res[2])
        except Exception as exc:
            # Handle Redis downtime according to route criticality
            if fail_closed or name in ("login", "register"):
                # Security Trade-off: Fail CLOSED for login/register vs. fail OPEN for general routes.
                # Authentication endpoints are prime targets for automated credential stuffing and
                # brute-force dictionary attacks. Allowing unthrottled authentication attempts during
                # a Redis outage would create a high-severity security window where attackers can hammer
                # user passwords undetected. We choose security over availability for authentication.
                logger.error(
                    "rate_limit_redis_unavailable_fail_closed",
                    name=name,
                    identifier=identifier,
                    error=str(exc),
                )
                raise RateLimitExceeded(
                    message="Rate limiter unavailable; login temporarily disabled for security.",
                    retry_after=window_seconds,
                    limit=limit,
                    remaining=0,
                    reset=int(now + window_seconds),
                ) from exc

            # General routes fail OPEN to preserve platform availability
            logger.warning(
                "rate_limit_redis_unavailable_fail_open",
                name=name,
                identifier=identifier,
                error=str(exc),
            )
            return

        if allowed == 0:
            retry_after = max(1, reset_time - int(now))
            logger.warning(
                "rate_limit_exceeded",
                name=name,
                identifier=identifier,
                limit=limit,
                retry_after=retry_after,
            )
            raise RateLimitExceeded(
                message="Too many requests. Please wait before retrying.",
                retry_after=retry_after,
                limit=limit,
                remaining=0,
                reset=reset_time,
            )

        # Apply rate limit headers to successful response and request state
        rate_headers = {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset_time),
        }
        request.state.rate_limit_headers = rate_headers
        response.headers.update(rate_headers)

    return _rate_limiter


async def check_ws_rate_limit(
    websocket: WebSocket,
    name: str = "ws_connect",
    limit: int = 20,
    window_seconds: int = 60,
) -> bool:
    """Validate WebSocket connection rate per client IP.

    Returns True if permitted, or False if limit is exceeded.
    """
    settings = get_settings()
    ip = get_client_ip(websocket, settings.TRUST_PROXY)
    redis_key = f"rate_limit:{name}:ip:{ip}"
    now = time.time()

    try:
        try:
            redis_client = get_redis()
        except RuntimeError:
            from app.core.redis import init_redis
            redis_client = await init_redis()

        res = await redis_client.eval(
            SLIDING_WINDOW_LUA,
            1,
            redis_key,
            str(now),
            str(window_seconds),
            str(limit),
        )
        allowed = int(res[0])
        return allowed == 1
    except Exception as exc:
        logger.warning(
            "ws_rate_limit_redis_error_fail_open",
            ip=ip,
            error=str(exc),
        )
        return True
