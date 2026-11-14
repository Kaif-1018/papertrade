"""Integration tests for PnL Analytics API endpoint (GET /api/analytics/pnl).

Verifies:
- 401 Unauthorized for unauthenticated requests
- Initial portfolio with no trades returns clean zero totals and null win rate
- End-to-end FIFO realised and unrealised PnL calculation via real trade execution
- Live price shifts: unrealised moves while realised remains fixed
- include_matches=true flag returns complete match breakdown with buy/sell trade IDs
- Symbol filtering isolates single-symbol performance
- Best and worst symbol identification and win rate percentage
- Stale or missing price fallback in registry
- Holding consistency sanity check detects DB holding vs FIFO mismatch
"""

from datetime import datetime, timezone
from decimal import Decimal
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.price_registry import clear, set_price


async def register_and_login(client: AsyncClient) -> tuple[str, str]:
    """Register a new user and return (token, user_email)."""
    email = f"analytics-{uuid.uuid4().hex[:8]}@test.com"
    reg = await client.post(
        "/api/auth/register",
        json={"email": email, "password": "TestPass123!"},
    )
    assert reg.status_code == 201

    login = await client.post(
        "/api/auth/login",
        json={"email": email, "password": "TestPass123!"},
    )
    assert login.status_code == 200
    return login.json()["access_token"], email


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def seed_price(symbol: str, price: str) -> None:
    set_price(
        symbol=symbol,
        price=Decimal(price),
        event_time=datetime.now(timezone.utc),
    )


@pytest.fixture(autouse=True)
def _clear_registry():
    clear()
    yield
    clear()


@pytest.mark.asyncio
async def test_get_analytics_pnl_unauthenticated(client: AsyncClient):
    """GET /api/analytics/pnl returns 401 without authentication."""
    resp = await client.get("/api/analytics/pnl")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_analytics_pnl_empty_trades(client: AsyncClient):
    """New user with no trades receives zero totals and null metrics."""
    token, _ = await register_and_login(client)

    resp = await client.get("/api/analytics/pnl", headers=auth_headers(token))
    assert resp.status_code == 200

    data = resp.json()
    assert data["total_realised_pnl"] == "0.00000000"
    assert data["total_unrealised_pnl"] == "0.00000000"
    assert data["total_trades"] == 0
    assert data["win_rate"] is None
    assert data["best_symbol"] is None
    assert data["worst_symbol"] is None
    assert data["consistency_warning"] is None
    assert data["symbols"] == []


@pytest.mark.asyncio
async def test_fifo_pnl_end_to_end_calculations(client: AsyncClient):
    """End-to-end execution of BUYs and SELL verifies FIFO realised and unrealised math."""
    token, _ = await register_and_login(client)
    headers = auth_headers(token)

    # 1. Buy 0.05 BTC @ 50,000 (cost = $2,500)
    seed_price("BTCUSDT", "50000.00000000")
    b1 = await client.post(
        "/api/trade/buy",
        headers=headers,
        json={"symbol": "BTCUSDT", "quantity": "0.05000000"},
    )
    assert b1.status_code == 201

    # 2. Buy 0.05 BTC @ 60,000 (cost = $3,000)
    seed_price("BTCUSDT", "60000.00000000")
    b2 = await client.post(
        "/api/trade/buy",
        headers=headers,
        json={"symbol": "BTCUSDT", "quantity": "0.05000000"},
    )
    assert b2.status_code == 201

    # 3. Sell 0.075 BTC @ 70,000 (proceeds = $5,250)
    # FIFO:
    # - Consumes full Lot 1 (0.05 @ 50k): PnL = 0.05 * (70k - 50k) = +1,000
    # - Consumes partial Lot 2 (0.025 @ 60k): PnL = 0.025 * (70k - 60k) = +250
    # Total Realised PnL = 1,250.00000000
    # Open Lot: 0.025 BTC @ 60,000 (cost basis = $1,500)
    # Current live price = 70,000 -> market value = $1,750
    # Unrealised PnL = 1,750 - 1,500 = +250.00000000
    seed_price("BTCUSDT", "70000.00000000")
    s1 = await client.post(
        "/api/trade/sell",
        headers=headers,
        json={"symbol": "BTCUSDT", "quantity": "0.07500000"},
    )
    assert s1.status_code == 201

    # Check analytics
    resp = await client.get("/api/analytics/pnl", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_realised_pnl"] == "1250.00000000"
    assert data["total_unrealised_pnl"] == "250.00000000"
    assert data["total_trades"] == 3
    assert data["win_rate"] == "100.0000"
    assert data["best_symbol"] == "BTCUSDT"
    assert data["worst_symbol"] == "BTCUSDT"
    assert data["consistency_warning"] is None

    assert len(data["symbols"]) == 1
    sym = data["symbols"][0]
    assert sym["symbol"] == "BTCUSDT"
    assert sym["realised_pnl"] == "1250.00000000"
    assert sym["unrealised_pnl"] == "250.00000000"
    assert sym["total_bought_qty"] == "0.10000000"
    assert sym["total_sold_qty"] == "0.07500000"
    assert sym["open_quantity"] == "0.02500000"
    assert sym["current_price"] == "70000.00000000"
    assert sym["stale_price"] is False
    assert sym["matches"] is None  # default include_matches=False


@pytest.mark.asyncio
async def test_live_price_shifts_unrealised_not_realised(client: AsyncClient):
    """Live price fluctuations adjust unrealised PnL but leave realised PnL unchanged."""
    token, _ = await register_and_login(client)
    headers = auth_headers(token)

    seed_price("BTCUSDT", "50000.00000000")
    await client.post("/api/trade/buy", headers=headers, json={"symbol": "BTCUSDT", "quantity": "0.05000000"})
    seed_price("BTCUSDT", "60000.00000000")
    await client.post("/api/trade/buy", headers=headers, json={"symbol": "BTCUSDT", "quantity": "0.05000000"})
    seed_price("BTCUSDT", "70000.00000000")
    await client.post("/api/trade/sell", headers=headers, json={"symbol": "BTCUSDT", "quantity": "0.07500000"})

    # Shift price upwards to 80,000:
    # Open qty is 0.025 BTC (cost basis = 1,500)
    # Market value = 0.025 * 80,000 = 2,000
    # New unrealised PnL = 2,000 - 1,500 = +500.00000000
    seed_price("BTCUSDT", "80000.00000000")

    resp = await client.get("/api/analytics/pnl", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_realised_pnl"] == "1250.00000000"  # Unchanged!
    assert data["total_unrealised_pnl"] == "500.00000000"  # Shifted!
    sym = data["symbols"][0]
    assert sym["realised_pnl"] == "1250.00000000"
    assert sym["unrealised_pnl"] == "500.00000000"
    assert sym["current_price"] == "80000.00000000"


@pytest.mark.asyncio
async def test_include_matches_flag(client: AsyncClient):
    """Passing include_matches=true returns detailed closed lots matches."""
    token, _ = await register_and_login(client)
    headers = auth_headers(token)

    seed_price("BTCUSDT", "50000.00000000")
    r_b1 = await client.post("/api/trade/buy", headers=headers, json={"symbol": "BTCUSDT", "quantity": "0.05000000"})
    buy1_id = r_b1.json()["trade"]["id"]

    seed_price("BTCUSDT", "60000.00000000")
    r_b2 = await client.post("/api/trade/buy", headers=headers, json={"symbol": "BTCUSDT", "quantity": "0.05000000"})
    buy2_id = r_b2.json()["trade"]["id"]

    seed_price("BTCUSDT", "70000.00000000")
    r_s1 = await client.post("/api/trade/sell", headers=headers, json={"symbol": "BTCUSDT", "quantity": "0.07500000"})
    sell1_id = r_s1.json()["trade"]["id"]

    resp = await client.get("/api/analytics/pnl?include_matches=true", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    matches = data["symbols"][0]["matches"]
    assert matches is not None
    assert len(matches) == 2

    # Match 1: fully consumed buy1
    m1 = matches[0]
    assert m1["sell_trade_id"] == sell1_id
    assert m1["buy_trade_id"] == buy1_id
    assert m1["quantity"] == "0.05000000"
    assert m1["buy_price"] == "50000.00000000"
    assert m1["sell_price"] == "70000.00000000"
    assert m1["pnl"] == "1000.00000000"

    # Match 2: partially consumed buy2
    m2 = matches[1]
    assert m2["sell_trade_id"] == sell1_id
    assert m2["buy_trade_id"] == buy2_id
    assert m2["quantity"] == "0.02500000"
    assert m2["buy_price"] == "60000.00000000"
    assert m2["sell_price"] == "70000.00000000"
    assert m2["pnl"] == "250.00000000"


@pytest.mark.asyncio
async def test_symbol_filtering_and_multi_symbol_metrics(client: AsyncClient):
    """Verifies best/worst symbol, win rate with losses, and ?symbol filtering."""
    token, _ = await register_and_login(client)
    headers = auth_headers(token)

    # BTCUSDT: gain of 1,250 (2 winning matches)
    seed_price("BTCUSDT", "50000.00000000")
    await client.post("/api/trade/buy", headers=headers, json={"symbol": "BTCUSDT", "quantity": "0.05000000"})
    seed_price("BTCUSDT", "60000.00000000")
    await client.post("/api/trade/buy", headers=headers, json={"symbol": "BTCUSDT", "quantity": "0.05000000"})
    seed_price("BTCUSDT", "70000.00000000")
    await client.post("/api/trade/sell", headers=headers, json={"symbol": "BTCUSDT", "quantity": "0.07500000"})

    # ETHUSDT: loss of 500 (1 losing match)
    seed_price("ETHUSDT", "3000.00000000")
    await client.post("/api/trade/buy", headers=headers, json={"symbol": "ETHUSDT", "quantity": "1.00000000"})
    seed_price("ETHUSDT", "2500.00000000")
    await client.post("/api/trade/sell", headers=headers, json={"symbol": "ETHUSDT", "quantity": "1.00000000"})

    # Global analytics
    resp = await client.get("/api/analytics/pnl", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_realised_pnl"] == "750.00000000"  # 1250 - 500
    assert data["total_trades"] == 5
    # 2 winning matches out of 3 total matches = 66.6667%
    assert data["win_rate"] == "66.6667"
    assert data["best_symbol"] == "BTCUSDT"
    assert data["worst_symbol"] == "ETHUSDT"
    assert len(data["symbols"]) == 2

    # Filter by symbol BTCUSDT
    r_btc = await client.get("/api/analytics/pnl?symbol=btcusdt", headers=headers)
    assert r_btc.status_code == 200
    data_btc = r_btc.json()
    assert len(data_btc["symbols"]) == 1
    assert data_btc["symbols"][0]["symbol"] == "BTCUSDT"

    # Filter by symbol ETHUSDT
    r_eth = await client.get("/api/analytics/pnl?symbol=ETHUSDT", headers=headers)
    assert r_eth.status_code == 200
    data_eth = r_eth.json()
    assert len(data_eth["symbols"]) == 1
    assert data_eth["symbols"][0]["symbol"] == "ETHUSDT"
    assert data_eth["symbols"][0]["realised_pnl"] == "-500.00000000"


@pytest.mark.asyncio
async def test_holding_consistency_warning(client: AsyncClient, db_session: AsyncSession):
    """When a holding row in the database is perturbed, a consistency warning is surfaced."""
    token, _ = await register_and_login(client)
    headers = auth_headers(token)

    seed_price("BTCUSDT", "50000.00000000")
    await client.post("/api/trade/buy", headers=headers, json={"symbol": "BTCUSDT", "quantity": "0.10000000"})

    # Check baseline consistency
    r1 = await client.get("/api/analytics/pnl", headers=headers)
    assert r1.status_code == 200
    assert r1.json()["consistency_warning"] is None

    # Directly perturb the database holding row to 0.05000000
    await db_session.execute(
        sa_text("UPDATE holdings SET quantity = 0.05000000 WHERE symbol = 'BTCUSDT'")
    )
    await db_session.commit()

    # Query analytics again
    r2 = await client.get("/api/analytics/pnl", headers=headers)
    assert r2.status_code == 200
    data2 = r2.json()
    assert data2["consistency_warning"] is not None
    assert "Holding quantity mismatch for BTCUSDT" in data2["consistency_warning"]
    assert "FIFO open quantity is 0.10000000" in data2["consistency_warning"]
    assert "database holding is 0.05000000" in data2["consistency_warning"]


@pytest.mark.asyncio
async def test_stale_or_missing_price_fallback(client: AsyncClient):
    """When a symbol's live price is missing from the registry, it falls back to cost basis."""
    token, _ = await register_and_login(client)
    headers = auth_headers(token)

    seed_price("BTCUSDT", "50000.00000000")
    await client.post("/api/trade/buy", headers=headers, json={"symbol": "BTCUSDT", "quantity": "0.10000000"})

    # Clear price registry so BTCUSDT is absent
    clear()

    resp = await client.get("/api/analytics/pnl", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    # Holding valued at cost basis: unrealised PnL is 0
    assert data["total_unrealised_pnl"] == "0.00000000"
    sym = data["symbols"][0]
    assert sym["stale_price"] is True
    assert sym["current_price"] is None
    assert sym["unrealised_pnl"] == "0.00000000"

