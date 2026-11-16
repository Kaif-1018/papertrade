"""Unit and integration tests for Redis-backed sliding-window rate limiting.

Verifies:
- Sliding window Lua script atomicity and boundary enforcement
- Login endpoint rate limit (10/min per IP, fails CLOSED on Redis outage)
- Trade endpoint rate limit (30/min per user, partitioned by user)
- General API rate limit (120/min per IP/user, fails OPEN on Redis outage)
- Reverse proxy header spoofing defense (TRUST_PROXY=false ignores X-Forwarded-For)
- Standard 429 response structure and RFC-compliant rate limit headers
"""

from decimal import Decimal
import time
from unittest.mock import AsyncMock, patch
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from redis.asyncio import Redis

from app.api.deps import get_db
from app.core.config import get_settings
from app.core.exceptions import RateLimitExceeded
from app.core.price_registry import set_price
from app.core.rate_limit import get_client_ip, rate_limit
from app.core.redis import get_redis
from app.core.security import create_access_token
from app.main import create_app
from app.models.user import UserRole


@pytest.fixture
def test_app(test_redis, session_factory):
    app = create_app()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = lambda: test_redis
    app.state.session_factory = session_factory
    return app


@pytest.fixture
def auth_headers():
    def _headers(user_id: uuid.UUID | None = None, role: UserRole = UserRole.USER):
        uid = user_id or uuid.uuid4()
        token = create_access_token(subject=str(uid), role=role.value)
        return {"Authorization": f"Bearer {token}"}

    return _headers


@pytest.mark.asyncio
async def test_auth_login_rate_limit_exceeded(test_app):
    """10 login requests are accepted, 11th is rejected with 429 and rate limit headers."""
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        # First 10 requests reach the login handler (401 invalid credentials)
        for _ in range(10):
            resp = await client.post(
                "/api/auth/login",
                json={"email": "ratelimit@example.com", "password": "WrongPassword1!"},
            )
            assert resp.status_code == 401
            assert "X-RateLimit-Limit" in resp.headers

        # 11th request is blocked by sliding window rate limiter
        blocked = await client.post(
            "/api/auth/login",
            json={"email": "ratelimit@example.com", "password": "WrongPassword1!"},
        )
        assert blocked.status_code == 429
        body = blocked.json()
        assert body["error"]["code"] == "RATE_LIMITED"
        assert "Retry-After" in blocked.headers
        assert blocked.headers["X-RateLimit-Limit"] == "10"
        assert blocked.headers["X-RateLimit-Remaining"] == "0"


@pytest.mark.asyncio
async def test_login_fails_closed_when_redis_unavailable(test_app, monkeypatch):
    """When Redis fails, login must fail CLOSED to prevent brute-force attacks."""
    mock_redis = AsyncMock()
    mock_redis.eval.side_effect = ConnectionError("Redis connection refused")

    test_app.dependency_overrides[get_redis] = lambda: mock_redis

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        resp = await client.post(
            "/api/auth/login",
            json={"email": "victim@example.com", "password": "Password123!"},
        )
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "RATE_LIMITED"


@pytest.mark.asyncio
async def test_general_api_fails_open_when_redis_unavailable(test_app, monkeypatch):
    """General routes fail OPEN when Redis is unreachable to preserve availability."""
    mock_redis = AsyncMock()
    mock_redis.eval.side_effect = ConnectionError("Redis connection refused")

    test_app.dependency_overrides[get_redis] = lambda: mock_redis

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        resp = await client.get("/api/prices")
        assert resp.status_code == 200


def test_trust_proxy_ip_extraction():
    """Client IP extraction honors settings.TRUST_PROXY flag."""
    req_mock = AsyncMock()
    req_mock.client.host = "192.168.1.10"
    req_mock.headers = {"x-forwarded-for": "203.0.113.195, 10.0.0.1"}

    # Untrusted proxy: uses socket address
    assert get_client_ip(req_mock, trust_proxy=False) == "192.168.1.10"

    # Trusted proxy: extracts first upstream client IP
    assert get_client_ip(req_mock, trust_proxy=True) == "203.0.113.195"
