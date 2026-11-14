"""Integration tests for Trade History API endpoint (GET /api/trades).

Verifies:
- Keyset cursor pagination walks through all pages without duplicates or gaps
- Keyset stability when new trades land between page fetches
- Filtering by symbol, trade type, and date range (alone and combined)
- User isolation: other users' trades never appear
- include_total parameter includes total_count only when requested
- Error handling:
  - 422 INVALID_CURSOR on malformed cursor strings
  - 422 INVALID_DATE_RANGE when from > to
  - 422 Validation error when limit > 100
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db
from app.core.security import create_access_token
from app.main import create_app
from app.models.trade import Trade, TradeType


@pytest.fixture
def api_app(session_factory):
    app = create_app()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = session_factory
    return app


def auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    token = create_access_token(str(user_id), role="USER")
    return {"Authorization": f"Bearer {token}"}


async def seed_user_trades(
    session_factory,
    user_id: uuid.UUID,
    count: int = 50,
    symbol: str = "BTCUSDT",
    base_time: datetime | None = None,
) -> list[Trade]:
    """Helper to commit a deterministic chronological sequence of trades."""
    start_time = base_time or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    trades = []
    async with session_factory() as session:
        for i in range(count):
            trade = Trade(
                id=uuid.uuid4(),
                user_id=user_id,
                symbol=symbol,
                trade_type=TradeType.BUY if i % 2 == 0 else TradeType.SELL,
                quantity=Decimal("0.10000000"),
                price=Decimal("60000.00000000"),
                total_value=Decimal("6000.00000000"),
                executed_at=start_time + timedelta(minutes=i),
            )
            session.add(trade)
            trades.append(trade)
        await session.commit()
    return trades


@pytest.mark.asyncio
async def test_keyset_pagination_full_walkthrough(clean_db, session_factory, create_user, api_app):
    """Pagination walks through all 45 trades in pages of 15 with zero duplicates or gaps."""
    user_id = await create_user()
    seeded = await seed_user_trades(session_factory, user_id, count=45)
    expected_ids_desc = [str(t.id) for t in sorted(seeded, key=lambda x: (x.executed_at, x.id), reverse=True)]

    all_fetched_ids = []
    cursor = None

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        page_count = 0
        while True:
            params = {"limit": 15}
            if cursor:
                params["cursor"] = cursor

            resp = await ac.get("/api/trades", params=params, headers=auth_headers(user_id))
            assert resp.status_code == 200
            data = resp.json()

            page_items = data["items"]
            page_count += 1
            all_fetched_ids.extend([item["id"] for item in page_items])

            if not data["has_more"]:
                assert data["next_cursor"] is None
                break

            assert data["next_cursor"] is not None
            cursor = data["next_cursor"]

        # Exactly 3 pages of 15
        assert page_count == 3
        assert len(all_fetched_ids) == 45
        assert all_fetched_ids == expected_ids_desc


@pytest.mark.asyncio
async def test_keyset_stability_with_interleaved_new_trades(clean_db, session_factory, create_user, api_app):
    """Keyset pagination remains stable when new trades land between page requests."""
    user_id = await create_user()
    base_time = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    seeded = await seed_user_trades(session_factory, user_id, count=25, base_time=base_time)

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        # Fetch page 1 (10 items)
        resp1 = await ac.get("/api/trades?limit=10", headers=auth_headers(user_id))
        assert resp1.status_code == 200
        page1 = resp1.json()
        assert len(page1["items"]) == 10
        cursor1 = page1["next_cursor"]
        page1_ids = [item["id"] for item in page1["items"]]

        # Insert 5 NEW trades with more recent timestamps (simulating trades while user reads page 1)
        newer_time = base_time + timedelta(hours=5)
        await seed_user_trades(session_factory, user_id, count=5, base_time=newer_time)

        # Fetch page 2 using cursor from page 1
        resp2 = await ac.get(f"/api/trades?limit=10&cursor={cursor1}", headers=auth_headers(user_id))
        assert resp2.status_code == 200
        page2 = resp2.json()
        assert len(page2["items"]) == 10
        page2_ids = [item["id"] for item in page2["items"]]

        # Page 2 must not contain any items from Page 1 (no duplicates)
        overlap = set(page1_ids).intersection(set(page2_ids))
        assert len(overlap) == 0

        # And items in page 2 match the expected next 10 items from initial seeded batch
        expected_page2_ids = [
            str(t.id) for t in sorted(seeded, key=lambda x: (x.executed_at, x.id), reverse=True)[10:20]
        ]
        assert page2_ids == expected_page2_ids


@pytest.mark.asyncio
async def test_filters_symbol_type_and_dates(clean_db, session_factory, create_user, api_app):
    """Test individual and combined filtering for symbol, type, and date bounds."""
    user_id = await create_user()
    t0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        # BTC trades
        session.add(Trade(id=uuid.uuid4(), user_id=user_id, symbol="BTCUSDT", trade_type=TradeType.BUY, quantity=Decimal("1"), price=Decimal("60000"), total_value=Decimal("60000"), executed_at=t0))
        session.add(Trade(id=uuid.uuid4(), user_id=user_id, symbol="BTCUSDT", trade_type=TradeType.SELL, quantity=Decimal("1"), price=Decimal("65000"), total_value=Decimal("65000"), executed_at=t0 + timedelta(hours=1)))
        # ETH trades
        session.add(Trade(id=uuid.uuid4(), user_id=user_id, symbol="ETHUSDT", trade_type=TradeType.BUY, quantity=Decimal("10"), price=Decimal("3000"), total_value=Decimal("30000"), executed_at=t0 + timedelta(hours=2)))
        session.add(Trade(id=uuid.uuid4(), user_id=user_id, symbol="ETHUSDT", trade_type=TradeType.SELL, quantity=Decimal("10"), price=Decimal("3200"), total_value=Decimal("32000"), executed_at=t0 + timedelta(hours=3)))
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        headers = auth_headers(user_id)

        # 1. Filter by symbol
        r_sym = await ac.get("/api/trades?symbol=btcusdt", headers=headers)
        assert r_sym.status_code == 200
        assert len(r_sym.json()["items"]) == 2
        assert all(i["symbol"] == "BTCUSDT" for i in r_sym.json()["items"])

        # 2. Filter by trade type
        r_type = await ac.get("/api/trades?type=BUY", headers=headers)
        assert r_type.status_code == 200
        assert len(r_type.json()["items"]) == 2
        assert all(i["trade_type"] == "BUY" for i in r_type.json()["items"])

        # 3. Filter by date bounds: inclusive start, exclusive end
        from_str = (t0 + timedelta(hours=1)).isoformat()
        to_str = (t0 + timedelta(hours=3)).isoformat()
        r_date = await ac.get("/api/trades", params={"from": from_str, "to": to_str}, headers=headers)
        assert r_date.status_code == 200
        items = r_date.json()["items"]
        assert len(items) == 2  # hours=1 (BTC SELL) and hours=2 (ETH BUY); hours=3 is excluded

        # 4. Combined filter
        r_comb = await ac.get(f"/api/trades?symbol=ETHUSDT&type=BUY", headers=headers)
        assert r_comb.status_code == 200
        assert len(r_comb.json()["items"]) == 1
        assert r_comb.json()["items"][0]["symbol"] == "ETHUSDT"
        assert r_comb.json()["items"][0]["trade_type"] == "BUY"


@pytest.mark.asyncio
async def test_user_isolation(clean_db, session_factory, create_user, api_app):
    """User A only sees User A trades; User B trades never leak."""
    user_a = await create_user()
    user_b = await create_user()

    await seed_user_trades(session_factory, user_a, count=5, symbol="BTCUSDT")
    await seed_user_trades(session_factory, user_b, count=8, symbol="ETHUSDT")

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        r_a = await ac.get("/api/trades", headers=auth_headers(user_a))
        r_b = await ac.get("/api/trades", headers=auth_headers(user_b))

        items_a = r_a.json()["items"]
        items_b = r_b.json()["items"]

        assert len(items_a) == 5
        assert all(i["symbol"] == "BTCUSDT" for i in items_a)

        assert len(items_b) == 8
        assert all(i["symbol"] == "ETHUSDT" for i in items_b)


@pytest.mark.asyncio
async def test_include_total_count(clean_db, session_factory, create_user, api_app):
    """include_total=true populates total_count; false leaves it null."""
    user_id = await create_user()
    await seed_user_trades(session_factory, user_id, count=12)

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        headers = auth_headers(user_id)

        # Without include_total
        r1 = await ac.get("/api/trades?limit=5", headers=headers)
        assert r1.status_code == 200
        assert r1.json()["total_count"] is None
        assert len(r1.json()["items"]) == 5

        # With include_total
        r2 = await ac.get("/api/trades?limit=5&include_total=true", headers=headers)
        assert r2.status_code == 200
        assert r2.json()["total_count"] == 12
        assert len(r2.json()["items"]) == 5


@pytest.mark.asyncio
async def test_error_handling(clean_db, session_factory, create_user, api_app):
    """Validate structured 422 errors for bad cursor, invalid date ranges, and oversized limit."""
    user_id = await create_user()
    headers = auth_headers(user_id)

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        # 1. Invalid cursor
        r_cursor = await ac.get("/api/trades?cursor=not-valid-base64!!!", headers=headers)
        assert r_cursor.status_code == 422
        assert r_cursor.json()["error"]["code"] == "INVALID_CURSOR"

        # 2. from > to date range
        t_early = "2026-01-01T10:00:00Z"
        t_late = "2026-01-02T10:00:00Z"
        r_dates = await ac.get(f"/api/trades?from={t_late}&to={t_early}", headers=headers)
        assert r_dates.status_code == 422
        assert r_dates.json()["error"]["code"] == "INVALID_DATE_RANGE"

        # 3. Limit > 100
        r_limit = await ac.get("/api/trades?limit=101", headers=headers)
        assert r_limit.status_code == 422
