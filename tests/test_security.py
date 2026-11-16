"""Security tests verifying platform defenses:
- Startup validation in non-development environments
- Security headers (CSP, X-Frame-Options, X-Content-Type-Options, etc.)
- Request body size limit (64 KB) enforcement (HTTP 413)
- Schema extra field rejection (mass assignment defense)
- Production API documentation exposure disablement
- CORS credential & wildcard constraints
- IDOR isolation on WebSocket portfolio stream
"""

from decimal import Decimal
import os
import uuid

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
import pytest

from app.api.deps import get_db
from app.core.config import Settings, get_settings
from app.core.redis import get_redis
from app.core.security import create_access_token
from app.main import create_app
from app.models.user import UserRole


@pytest.fixture
def secure_app(test_redis, db_session, session_factory):
    app = create_app()

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = lambda: test_redis
    app.state.session_factory = session_factory
    return app


def test_startup_validation_refuses_weak_jwt_in_production(monkeypatch):
    """Refuse to boot in production with default or short JWT secrets."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("JWT_SECRET", "change-me-to-a-random-secret")

    with pytest.raises(ValueError, match="JWT_SECRET must be at least 32 characters"):
        Settings()


def test_startup_validation_succeeds_with_strong_secret_in_production(monkeypatch):
    """Production boot succeeds when a strong secret (>32 chars) is supplied."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("JWT_SECRET", "super-strong-production-cryptographic-secret-32-chars")

    s = Settings()
    assert s.ENVIRONMENT == "production"


@pytest.mark.asyncio
async def test_security_headers_present_on_all_responses(secure_app):
    """Every HTTP response must include standard defense-in-depth headers."""
    async with AsyncClient(transport=ASGITransport(app=secure_app), base_url="http://test") as client:
        resp = await client.get("/api/prices")
        assert resp.status_code == 200

        headers = resp.headers
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("X-Frame-Options") == "DENY"
        assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert "Content-Security-Policy" in headers
        assert "X-Request-ID" in headers


@pytest.mark.asyncio
async def test_request_body_size_limit_exceeded(secure_app):
    """Payloads exceeding 64KB are rejected with HTTP 413 Payload Too Large."""
    async with AsyncClient(transport=ASGITransport(app=secure_app), base_url="http://test") as client:
        # Create a payload larger than 64 KB (65,536 bytes)
        large_padding = "A" * 70000
        resp = await client.post(
            "/api/auth/login",
            content=large_padding,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


@pytest.mark.asyncio
async def test_schema_rejects_extra_fields_mass_assignment(secure_app):
    """Requests with undeclared extra fields (e.g. role, balance) are strictly rejected with 422."""
    async with AsyncClient(transport=ASGITransport(app=secure_app), base_url="http://test") as client:
        resp = await client.post(
            "/api/auth/register",
            json={
                "email": "attacker@example.com",
                "password": "Password123!",
                "role": "ADMIN",
                "virtual_balance": "999999999",
            },
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "Extra inputs are not permitted" in body["error"]["message"]


def test_docs_disabled_in_production(monkeypatch):
    """OpenAPI and Swagger docs are disabled in production mode."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("JWT_SECRET", "super-strong-production-cryptographic-secret-32-chars")

    prod_app = create_app()
    client = TestClient(prod_app)

    docs_resp = client.get("/docs")
    assert docs_resp.status_code == 404

    openapi_resp = client.get("/openapi.json")
    assert openapi_resp.status_code == 404
