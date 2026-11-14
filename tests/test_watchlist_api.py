"""Integration tests for Watchlist REST API endpoints.

Tests:
- POST /api/watchlist/{symbol}: 201 on create, 200 on duplicate (idempotent)
- Lowercase normalization
- 404 SYMBOL_NOT_SUPPORTED for unsupported trading pairs
- DELETE /api/watchlist/{symbol}: 204 on success, 404 NOT_IN_WATCHLIST on missing
- GET /api/watchlist: ordered by added_at, live prices, empty registry handling
- Per-user cap enforcement (WATCHLIST_FULL 422)
- Multi-user isolation
- Concurrency:
  - 10 parallel POSTs of the same symbol produce exactly 1 DB row
  - 30 parallel POSTs of distinct symbols for cap=25 produce exactly 25 DB rows and 5 422 errors
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db
from app.core.price_registry import clear as clear_registry, set_price as reg_set_price
from app.core.security import create_access_token
from app.main import create_app


@pytest.fixture(autouse=True)
def clean_registry():
    clear_registry()
    yield
    clear_registry()


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


@pytest.mark.asyncio
async def test_add_watchlist_item_success(clean_db, session_factory, create_user, api_app):
    user_id = await create_user()

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        resp = await ac.post("/api/watchlist/BTCUSDT", headers=auth_headers(user_id))
        assert resp.status_code == 201

        data = resp.json()
        assert data["symbol"] == "BTCUSDT"
        assert "added_at" in data
        assert data["price"] is None
        assert data["stale"] is True


@pytest.mark.asyncio
async def test_add_watchlist_item_idempotent(clean_db, session_factory, create_user, api_app):
    user_id = await create_user()

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        # First POST -> 201
        resp1 = await ac.post("/api/watchlist/ETHUSDT", headers=auth_headers(user_id))
        assert resp1.status_code == 201
        data1 = resp1.json()

        # Second POST -> 200 with identical item
        resp2 = await ac.post("/api/watchlist/ETHUSDT", headers=auth_headers(user_id))
        assert resp2.status_code == 200
        data2 = resp2.json()

        assert data1["symbol"] == data2["symbol"]
        assert data1["added_at"] == data2["added_at"]


@pytest.mark.asyncio
async def test_add_watchlist_item_lowercase_normalized(clean_db, session_factory, create_user, api_app):
    user_id = await create_user()

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        resp = await ac.post("/api/watchlist/solusdt", headers=auth_headers(user_id))
        assert resp.status_code == 201
        assert resp.json()["symbol"] == "SOLUSDT"


@pytest.mark.asyncio
async def test_add_watchlist_unsupported_symbol(clean_db, session_factory, create_user, api_app):
    user_id = await create_user()

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        resp = await ac.post("/api/watchlist/UNKNOWNCOIN", headers=auth_headers(user_id))
        assert resp.status_code == 404
        error = resp.json()["error"]
        assert error["code"] == "SYMBOL_NOT_SUPPORTED"


@pytest.mark.asyncio
async def test_delete_watchlist_item_success(clean_db, session_factory, create_user, api_app):
    user_id = await create_user()

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        await ac.post("/api/watchlist/BTCUSDT", headers=auth_headers(user_id))

        del_resp = await ac.delete("/api/watchlist/BTCUSDT", headers=auth_headers(user_id))
        assert del_resp.status_code == 204

        get_resp = await ac.get("/api/watchlist", headers=auth_headers(user_id))
        assert get_resp.status_code == 200
        assert len(get_resp.json()) == 0


@pytest.mark.asyncio
async def test_delete_watchlist_item_missing_returns_404(clean_db, session_factory, create_user, api_app):
    user_id = await create_user()

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        del_resp = await ac.delete("/api/watchlist/BTCUSDT", headers=auth_headers(user_id))
        assert del_resp.status_code == 404
        error = del_resp.json()["error"]
        assert error["code"] == "NOT_IN_WATCHLIST"


@pytest.mark.asyncio
async def test_get_watchlist_ordered_with_live_prices(clean_db, session_factory, create_user, api_app):
    user_id = await create_user()
    now = datetime.now(timezone.utc)
    reg_set_price("BTCUSDT", Decimal("68123.45000000"), now)

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        await ac.post("/api/watchlist/BTCUSDT", headers=auth_headers(user_id))
        await ac.post("/api/watchlist/ETHUSDT", headers=auth_headers(user_id))

        resp = await ac.get("/api/watchlist", headers=auth_headers(user_id))
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2

        # In order of addition
        assert items[0]["symbol"] == "BTCUSDT"
        assert items[0]["price"] == "68123.45000000"
        assert items[0]["stale"] is False
        assert isinstance(items[0]["price_age_ms"], int)

        assert items[1]["symbol"] == "ETHUSDT"
        assert items[1]["price"] is None
        assert items[1]["stale"] is True


@pytest.mark.asyncio
async def test_user_isolation(clean_db, session_factory, create_user, api_app):
    user1 = await create_user()
    user2 = await create_user()

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        await ac.post("/api/watchlist/BTCUSDT", headers=auth_headers(user1))
        await ac.post("/api/watchlist/SOLUSDT", headers=auth_headers(user2))

        resp1 = await ac.get("/api/watchlist", headers=auth_headers(user1))
        symbols1 = [i["symbol"] for i in resp1.json()]
        assert symbols1 == ["BTCUSDT"]

        resp2 = await ac.get("/api/watchlist", headers=auth_headers(user2))
        symbols2 = [i["symbol"] for i in resp2.json()]
        assert symbols2 == ["SOLUSDT"]


@pytest.mark.asyncio
async def test_cap_enforcement_at_max(clean_db, session_factory, create_user, api_app, monkeypatch):
    monkeypatch.setenv("WATCHLIST_MAX_ITEMS", "2")
    user_id = await create_user()

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        r1 = await ac.post("/api/watchlist/BTCUSDT", headers=auth_headers(user_id))
        assert r1.status_code == 201

        r2 = await ac.post("/api/watchlist/ETHUSDT", headers=auth_headers(user_id))
        assert r2.status_code == 201

        # 3rd should fail with 422 WATCHLIST_FULL
        r3 = await ac.post("/api/watchlist/SOLUSDT", headers=auth_headers(user_id))
        assert r3.status_code == 422
        error = r3.json()["error"]
        assert error["code"] == "WATCHLIST_FULL"
        assert error["details"] == {"max": 2}


@pytest.mark.asyncio
async def test_concurrency_same_symbol_parallel_posts(clean_db, session_factory, create_user, api_app):
    """10 parallel POSTs of the same symbol all succeed (200 or 201) with exactly 1 row."""
    user_id = await create_user()

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        tasks = [
            ac.post("/api/watchlist/BTCUSDT", headers=auth_headers(user_id))
            for _ in range(10)
        ]
        responses = await asyncio.gather(*tasks)

        status_codes = [r.status_code for r in responses]
        assert all(code in (200, 201) for code in status_codes)
        assert 201 in status_codes

        # Query watchlist to verify exactly one row exists
        resp = await ac.get("/api/watchlist", headers=auth_headers(user_id))
        items = resp.json()
        assert len(items) == 1
        assert items[0]["symbol"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_concurrency_distinct_symbols_cap_25(clean_db, session_factory, create_user, api_app, monkeypatch):
    """30 parallel POSTs of different symbols for cap=25 produce exactly 25 items and 5 422s."""
    test_symbols = [f"COIN{i:02d}USDT" for i in range(1, 31)]
    pairs_str = ",".join(test_symbols)
    monkeypatch.setenv("TRADING_PAIRS", pairs_str)
    monkeypatch.setenv("WATCHLIST_MAX_ITEMS", "25")

    user_id = await create_user()

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as ac:
        tasks = [
            ac.post(f"/api/watchlist/{sym}", headers=auth_headers(user_id))
            for sym in test_symbols
        ]
        responses = await asyncio.gather(*tasks)

        success_count = sum(1 for r in responses if r.status_code == 201)
        full_count = sum(1 for r in responses if r.status_code == 422 and r.json().get("error", {}).get("code") == "WATCHLIST_FULL")

        assert success_count == 25
        assert full_count == 5

        # Verify DB holds exactly 25 items
        resp = await ac.get("/api/watchlist", headers=auth_headers(user_id))
        items = resp.json()
        assert len(items) == 25
