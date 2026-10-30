"""Day 3 — Authentication & Authorization test suite.

Tests registration, duplicate email handling, login validation, token issuance,
protected /me endpoint, expired tokens, refresh token rotation, token reuse
detection & emergency session revocation, and role-based access control.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid

import jwt
import pytest
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.security import create_access_token, create_refresh_token, hash_password
from app.models.user import User, UserRole
from app.repositories.user import UserRepository

# ---------------------------------------------------------------------------
# Registration Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_success(client):
    """Registering a new user succeeds with 201 and returns initial balance."""
    payload = {
        "email": "trader1@example.com",
        "password": "Password123!",
    }
    response = await client.post("/api/auth/register", json=payload)
    assert response.status_code == 201

    data = response.json()
    assert data["email"] == "trader1@example.com"
    assert data["role"] == "USER"
    assert data["virtual_balance"] == "10000.00000000"
    assert data["initial_balance"] == "10000.00000000"
    assert "id" in data
    assert "created_at" in data
    assert "password" not in data
    assert "password_hash" not in data


@pytest.mark.asyncio
async def test_register_duplicate_email(client):
    """Registering an existing email returns 409 USER_ALREADY_EXISTS."""
    payload = {
        "email": "duplicate@example.com",
        "password": "Password123!",
    }
    first_resp = await client.post("/api/auth/register", json=payload)
    assert first_resp.status_code == 201

    second_resp = await client.post("/api/auth/register", json=payload)
    assert second_resp.status_code == 409
    data = second_resp.json()
    assert data["error"]["code"] == "USER_ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_register_validation_short_password(client):
    """Password shorter than 8 characters is rejected with 422."""
    payload = {
        "email": "shortpass@example.com",
        "password": "short",
    }
    response = await client.post("/api/auth/register", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_register_validation_invalid_email(client):
    """Malformed email is rejected with 422."""
    payload = {
        "email": "not-an-email",
        "password": "Password123!",
    }
    response = await client.post("/api/auth/register", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Login Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_success(client, test_redis: Redis):
    """Logging in with correct credentials returns token pair and tracks jti in Redis."""
    # 1. Register
    reg_payload = {"email": "loginuser@example.com", "password": "SecurePassword123"}
    await client.post("/api/auth/register", json=reg_payload)

    # 2. Login
    login_resp = await client.post("/api/auth/login", json=reg_payload)
    assert login_resp.status_code == 200
    tokens = login_resp.json()
    assert "access_token" in tokens
    assert "refresh_token" in tokens
    assert tokens["token_type"] == "bearer"

    # 3. Verify refresh token was stored in Redis
    settings = get_settings()
    decoded_refresh = jwt.decode(
        tokens["refresh_token"], settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
    )
    user_id = decoded_refresh["sub"]
    jti = decoded_refresh["jti"]
    assert await test_redis.exists(f"refresh:{user_id}:{jti}") == 1


@pytest.mark.asyncio
async def test_login_wrong_password(client):
    """Wrong password returns generic 401 INVALID_CREDENTIALS."""
    reg_payload = {"email": "wrongpass@example.com", "password": "CorrectPassword123"}
    await client.post("/api/auth/register", json=reg_payload)

    login_resp = await client.post(
        "/api/auth/login",
        json={"email": "wrongpass@example.com", "password": "IncorrectPassword123"},
    )
    assert login_resp.status_code == 401
    assert login_resp.json()["error"]["code"] == "INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_login_nonexistent_user(client):
    """Non-existent email returns generic 401 INVALID_CREDENTIALS."""
    login_resp = await client.post(
        "/api/auth/login",
        json={"email": "ghost@example.com", "password": "SomePassword123"},
    )
    assert login_resp.status_code == 401
    assert login_resp.json()["error"]["code"] == "INVALID_CREDENTIALS"


# ---------------------------------------------------------------------------
# Current User (/me) Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_me_success(client):
    """GET /api/auth/me returns current user profile when authenticated."""
    reg_payload = {"email": "meuser@example.com", "password": "Password123!"}
    reg_resp = await client.post("/api/auth/register", json=reg_payload)
    user_id = reg_resp.json()["id"]

    login_resp = await client.post("/api/auth/login", json=reg_payload)
    access_token = login_resp.json()["access_token"]

    me_resp = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert me_resp.status_code == 200
    me_data = me_resp.json()
    assert me_data["id"] == user_id
    assert me_data["email"] == "meuser@example.com"
    assert me_data["virtual_balance"] == "10000.00000000"


@pytest.mark.asyncio
async def test_get_me_missing_token(client):
    """GET /api/auth/me without authorization header returns 401."""
    me_resp = await client.get("/api/auth/me")
    assert me_resp.status_code == 401
    assert me_resp.json()["error"]["code"] == "TOKEN_INVALID"


@pytest.mark.asyncio
async def test_get_me_expired_token(client, db_session):
    """GET /api/auth/me with an expired access token returns 401 TOKEN_EXPIRED."""
    user_repo = UserRepository(db_session)
    user = user_repo.create(
        email="expired@example.com",
        password_hash=hash_password("Password123!"),
        virtual_balance=Decimal("10000"),
        initial_balance=Decimal("10000"),
    )
    await db_session.commit()
    await db_session.refresh(user)

    # Generate token that expired 1 hour ago
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expired_payload = {
        "sub": str(user.id),
        "role": user.role.value,
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": now - timedelta(hours=2),
        "exp": now - timedelta(hours=1),
    }
    expired_token = jwt.encode(
        expired_payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM
    )

    resp = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_EXPIRED"


# ---------------------------------------------------------------------------
# Refresh Token Rotation & Reuse Detection Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_token_rotation(client, test_redis: Redis):
    """Refreshing a token returns a new pair and deletes the old refresh token."""
    reg_payload = {"email": "rotator@example.com", "password": "Password123!"}
    await client.post("/api/auth/register", json=reg_payload)

    login_resp = await client.post("/api/auth/login", json=reg_payload)
    initial_refresh = login_resp.json()["refresh_token"]

    settings = get_settings()
    old_claims = jwt.decode(
        initial_refresh, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
    )
    old_key = f"refresh:{old_claims['sub']}:{old_claims['jti']}"

    # Confirm old token exists in Redis
    assert await test_redis.exists(old_key) == 1

    # Perform refresh
    refresh_resp = await client.post(
        "/api/auth/refresh",
        json={"refresh_token": initial_refresh},
    )
    assert refresh_resp.status_code == 200
    new_tokens = refresh_resp.json()
    new_refresh = new_tokens["refresh_token"]
    assert new_refresh != initial_refresh

    # Old key is deleted
    assert await test_redis.exists(old_key) == 0

    # New key exists
    new_claims = jwt.decode(
        new_refresh, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
    )
    new_key = f"refresh:{new_claims['sub']}:{new_claims['jti']}"
    assert await test_redis.exists(new_key) == 1

    # New access token works
    me_resp = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {new_tokens['access_token']}"},
    )
    assert me_resp.status_code == 200


@pytest.mark.asyncio
async def test_refresh_token_reuse_detection(client, test_redis: Redis):
    """Presenting an already-used refresh token revokes all user sessions."""
    reg_payload = {"email": "reuser@example.com", "password": "Password123!"}
    await client.post("/api/auth/register", json=reg_payload)

    login_resp = await client.post("/api/auth/login", json=reg_payload)
    t1_refresh = login_resp.json()["refresh_token"]

    # First rotation: t1 -> t2
    refresh1_resp = await client.post(
        "/api/auth/refresh",
        json={"refresh_token": t1_refresh},
    )
    assert refresh1_resp.status_code == 200
    t2_tokens = refresh1_resp.json()
    t2_refresh = t2_tokens["refresh_token"]

    settings = get_settings()
    claims = jwt.decode(
        t2_refresh, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
    )
    user_id = claims["sub"]
    t2_key = f"refresh:{user_id}:{claims['jti']}"
    assert await test_redis.exists(t2_key) == 1

    # ATTACK / REUSE: Attacker attempts to use t1 again
    reuse_resp = await client.post(
        "/api/auth/refresh",
        json={"refresh_token": t1_refresh},
    )
    assert reuse_resp.status_code == 401
    assert reuse_resp.json()["error"]["code"] == "TOKEN_REUSE_DETECTED"

    # All sessions for user must be wiped from Redis
    assert await test_redis.exists(t2_key) == 0

    # Even the legitimate newest token t2 must now be rejected
    subsequent_resp = await client.post(
        "/api/auth/refresh",
        json={"refresh_token": t2_refresh},
    )
    assert subsequent_resp.status_code == 401


# ---------------------------------------------------------------------------
# Cross-Token Rejection & Logout Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reject_access_token_as_refresh_token(client):
    """Providing an access token to /refresh endpoint is rejected with 401."""
    reg_payload = {"email": "crosstoken@example.com", "password": "Password123!"}
    await client.post("/api/auth/register", json=reg_payload)
    login_resp = await client.post("/api/auth/login", json=reg_payload)
    access_token = login_resp.json()["access_token"]

    resp = await client.post(
        "/api/auth/refresh",
        json={"refresh_token": access_token},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_INVALID"


@pytest.mark.asyncio
async def test_reject_refresh_token_on_protected_endpoint(client):
    """Providing a refresh token to Bearer Authorization is rejected with 401."""
    reg_payload = {"email": "crossbearer@example.com", "password": "Password123!"}
    await client.post("/api/auth/register", json=reg_payload)
    login_resp = await client.post("/api/auth/login", json=reg_payload)
    refresh_token = login_resp.json()["refresh_token"]

    resp = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {refresh_token}"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_INVALID"


@pytest.mark.asyncio
async def test_logout(client, test_redis: Redis):
    """POST /api/auth/logout invalidates the refresh token in Redis."""
    reg_payload = {"email": "logoutuser@example.com", "password": "Password123!"}
    await client.post("/api/auth/register", json=reg_payload)
    login_resp = await client.post("/api/auth/login", json=reg_payload)
    refresh_token = login_resp.json()["refresh_token"]

    settings = get_settings()
    claims = jwt.decode(
        refresh_token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM]
    )
    key = f"refresh:{claims['sub']}:{claims['jti']}"
    assert await test_redis.exists(key) == 1

    logout_resp = await client.post(
        "/api/auth/logout",
        json={"refresh_token": refresh_token},
    )
    assert logout_resp.status_code == 200
    assert await test_redis.exists(key) == 0


# ---------------------------------------------------------------------------
# Role-Based Access Control Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_route_forbidden_for_user(client):
    """A user with USER role hitting an admin-only route gets 403 FORBIDDEN."""
    reg_payload = {"email": "regular@example.com", "password": "Password123!"}
    await client.post("/api/auth/register", json=reg_payload)
    login_resp = await client.post("/api/auth/login", json=reg_payload)
    access_token = login_resp.json()["access_token"]

    admin_resp = await client.get(
        "/api/admin/stats",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert admin_resp.status_code == 403
    assert admin_resp.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_admin_route_allowed_for_admin(client, db_session):
    """A user with ADMIN role can access admin-only routes."""
    user_repo = UserRepository(db_session)
    admin_user = user_repo.create(
        email="superadmin@example.com",
        password_hash=hash_password("SuperSecret123!"),
        virtual_balance=Decimal("50000"),
        initial_balance=Decimal("50000"),
        role=UserRole.ADMIN,
    )
    await db_session.commit()
    await db_session.refresh(admin_user)

    login_resp = await client.post(
        "/api/auth/login",
        json={"email": "superadmin@example.com", "password": "SuperSecret123!"},
    )
    assert login_resp.status_code == 200
    access_token = login_resp.json()["access_token"]

    admin_resp = await client.get(
        "/api/admin/stats",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert admin_resp.status_code == 200
    assert admin_resp.json()["status"] == "ok"

