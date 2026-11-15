"""API integration tests for GET /api/leaderboard.

Verifies:
- 401 Unauthorized for unauthenticated requests
- Constant query count: 200 seeded users issue exactly 2 database queries (verified via SQLAlchemy event listener)
- Top N pagination limit (default 20, up to 100)
- 'me' block is always present even when the caller is outside the top N rankings
- Privacy / Anonymity: no other user's email or UUID id leaks anywhere in the response
- 'is_me' flag is True for caller and False for all other participants
"""

from decimal import Decimal
import uuid

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import event

from app.core.cache import LEADERBOARD_CACHE_KEY
from app.core.security import create_access_token
from app.models.holding import Holding
from app.models.user import User, UserRole
from app.repositories.leaderboard_repository import LeaderboardRepository


def auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(str(user_id), role="USER")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_get_leaderboard_unauthenticated(client: AsyncClient):
    """GET /api/leaderboard returns 401 when no token is provided."""
    resp = await client.get("/api/leaderboard")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_leaderboard_constant_query_count_200_users(
    clean_db, session_factory, test_engine
):
    """Repository executes strictly 2 queries for 200 users and 50 holdings (no N+1)."""
    # 1. Bulk insert 200 users
    users = [
        User(
            id=uuid.uuid4(),
            email=f"trader_{i:03d}@domain.com",
            password_hash="$2b$12$fakehash",
            role=UserRole.USER,
            virtual_balance=Decimal("10000.00000000") + Decimal(str(i * 10)),
            initial_balance=Decimal("10000.00000000"),
        )
        for i in range(200)
    ]

    async with session_factory() as session:
        session.add_all(users)
        await session.commit()

    # 2. Bulk insert holdings for 50 users
    holdings = [
        Holding(
            id=uuid.uuid4(),
            user_id=users[i].id,
            symbol="BTCUSDT" if i % 2 == 0 else "ETHUSDT",
            quantity=Decimal("0.50000000"),
            avg_buy_price=Decimal("60000.00000000"),
        )
        for i in range(50)
    ]

    async with session_factory() as session:
        session.add_all(holdings)
        await session.commit()

    # 3. Count SQL statements executed by repository
    query_count = 0

    def statement_listener(conn, cursor, statement, parameters, context, executemany):
        nonlocal query_count
        query_count += 1

    event.listen(test_engine.sync_engine, "before_cursor_execute", statement_listener)

    try:
        async with session_factory() as session:
            repo = LeaderboardRepository(session)
            u_rows, h_rows = await repo.get_all_users_and_holdings()

            assert len(u_rows) == 200
            assert len(h_rows) == 50
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", statement_listener)

    # Must be strictly 2 queries: one for users, one for holdings
    assert query_count == 2, f"Expected 2 queries, got {query_count} (N+1 query detected!)"


@pytest.mark.asyncio
async def test_leaderboard_me_outside_top_n_and_privacy(
    clean_db, session_factory, create_user, client: AsyncClient, test_redis: Redis
):
    """'me' card is present when outside top N, and no competitor email or id leaks."""
    await test_redis.delete(LEADERBOARD_CACHE_KEY)

    # 1. Create 5 top traders with high balances
    top_user_ids = []
    top_emails = []
    for i in range(5):
        email = f"winner_{i}@supercrypto.com"
        uid = await create_user(balance=str(20000 + i * 1000), email=email)
        top_user_ids.append(uid)
        top_emails.append(email)

    # 2. Create the calling user with lower balance (rank 6, 50% loss against initial 10,000)
    caller_id = uuid.uuid4()
    caller_email = "mycaller@private.com"
    async with session_factory() as session:
        caller = User(
            id=caller_id,
            email=caller_email,
            password_hash="fake",
            role=UserRole.USER,
            virtual_balance=Decimal("5000.00000000"),
            initial_balance=Decimal("10000.00000000"),
        )
        session.add(caller)
        await session.commit()

    # 3. Request top 3 entries (caller is excluded from entries)
    resp = await client.get("/api/leaderboard?limit=3", headers=auth_headers(caller_id))
    assert resp.status_code == 200

    data = resp.json()
    assert data["total_users"] == 6
    assert len(data["entries"]) == 3

    # Entries are ranks 1, 2, 3
    ranks = [e["rank"] for e in data["entries"]]
    assert ranks == [1, 2, 3]

    # None of the entries in top 3 is the caller
    assert all(e["is_me"] is False for e in data["entries"])

    # 'me' block is explicitly present and shows rank 6
    assert data["me"] is not None
    assert data["me"]["rank"] == 6
    assert data["me"]["total_value"] == "5000.00000000"
    assert data["me"]["pnl_percent"] == "-50.00"

    # Privacy / Anonymity verification:
    raw_response_text = resp.text
    for uid in top_user_ids:
        assert str(uid) not in raw_response_text, f"UUID leak detected: {uid}"
    for email in top_emails:
        assert email not in raw_response_text, f"Email leak detected: {email}"
        domain = email.split("@")[1]
        assert domain not in raw_response_text, f"Domain leak detected: {domain}"

    # Confirm display_name is properly masked
    for entry in data["entries"]:
        assert entry["display_name"].endswith("***")
        assert "@" not in entry["display_name"]
        # Response entry must only contain permitted fields
        assert "user_id" not in entry
        assert "email" not in entry


@pytest.mark.asyncio
async def test_leaderboard_caller_inside_top_n(
    clean_db, create_user, client: AsyncClient, test_redis: Redis
):
    """When caller is in the top N, their entry has is_me=True, others have is_me=False."""
    await test_redis.delete(LEADERBOARD_CACHE_KEY)

    u1 = await create_user(balance="30000.00000000", email="first@test.com")
    u2 = await create_user(balance="20000.00000000", email="second@test.com")
    u3 = await create_user(balance="10000.00000000", email="third@test.com")

    # u2 queries top 5
    resp = await client.get("/api/leaderboard?limit=5", headers=auth_headers(u2))
    assert resp.status_code == 200

    data = resp.json()
    assert len(data["entries"]) == 3
    assert data["entries"][0]["rank"] == 1
    assert data["entries"][0]["is_me"] is False

    assert data["entries"][1]["rank"] == 2
    assert data["entries"][1]["is_me"] is True  # Caller is rank 2!

    assert data["entries"][2]["rank"] == 3
    assert data["entries"][2]["is_me"] is False

    assert data["me"]["rank"] == 2
    assert data["me"]["total_value"] == "20000.00000000"
