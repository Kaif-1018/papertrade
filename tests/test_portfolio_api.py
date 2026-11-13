"""API integration tests for GET /api/portfolio.

Verifies:
- 401 Unauthorized for unauthenticated requests
- Initial portfolio for fresh users (10,000 cash, 0 PnL, empty holdings)
- Holdings and totals update correctly after a BUY trade
- Real-time price shifts immediately reflect in portfolio valuation
- Stale price fallback to cost basis preserves overall valuation
"""

from datetime import datetime, timezone
from decimal import Decimal
import uuid

from httpx import AsyncClient
import pytest

from app.core.price_registry import clear, set_price


async def register_and_login(client: AsyncClient) -> tuple[str, dict]:
    """Register a new user and return (access_token, user_info)."""
    email = f"trader-{uuid.uuid4().hex[:8]}@test.com"
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
    token = login.json()["access_token"]
    return token, {"email": email}


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
async def test_get_portfolio_unauthenticated(client: AsyncClient):
    """GET /api/portfolio returns 401 when no token is supplied."""
    resp = await client.get("/api/portfolio")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_portfolio_new_user(client: AsyncClient):
    """New user starts with default cash (10,000.00000000), zero PnL, and no holdings."""
    token, _ = await register_and_login(client)

    resp = await client.get("/api/portfolio", headers=auth_headers(token))
    assert resp.status_code == 200

    data = resp.json()
    assert data["cash_balance"] == "10000.00000000"
    assert data["holdings_value"] == "0.00000000"
    assert data["total_value"] == "10000.00000000"
    assert data["initial_balance"] == "10000.00000000"
    assert data["total_pnl"] == "0.00000000"
    assert data["total_pnl_percent"] == "0.0000"
    assert data["has_stale_prices"] is False
    assert data["holdings"] == []
    assert "as_of" in data


@pytest.mark.asyncio
async def test_get_portfolio_after_buy(client: AsyncClient):
    """Buying 0.1 BTC at 50,000 updates cash balance and creates holding row."""
    token, _ = await register_and_login(client)

    seed_price("BTCUSDT", "50000.00000000")

    # Buy 0.1 BTC = 5,000 USD
    buy_resp = await client.post(
        "/api/trade/buy",
        headers=auth_headers(token),
        json={"symbol": "BTCUSDT", "quantity": "0.10000000"},
    )
    assert buy_resp.status_code == 201

    # Check portfolio valuation
    resp = await client.get("/api/portfolio", headers=auth_headers(token))
    assert resp.status_code == 200

    data = resp.json()
    assert data["cash_balance"] == "5000.00000000"
    assert data["holdings_value"] == "5000.00000000"
    assert data["total_value"] == "10000.00000000"
    assert data["total_pnl"] == "0.00000000"
    assert data["total_pnl_percent"] == "0.0000"
    assert len(data["holdings"]) == 1

    h = data["holdings"][0]
    assert h["symbol"] == "BTCUSDT"
    assert h["quantity"] == "0.10000000"
    assert h["avg_buy_price"] == "50000.00000000"
    assert h["current_price"] == "50000.00000000"
    assert h["cost_basis"] == "5000.00000000"
    assert h["market_value"] == "5000.00000000"
    assert h["unrealised_pnl"] == "0.00000000"
    assert h["pnl_percent"] == "0.0000"
    assert h["stale"] is False


@pytest.mark.asyncio
async def test_price_change_alters_portfolio_valuation(client: AsyncClient):
    """When market price increases, unrealised PnL and total value increase accordingly."""
    token, _ = await register_and_login(client)

    seed_price("BTCUSDT", "50000.00000000")
    buy_resp = await client.post(
        "/api/trade/buy",
        headers=auth_headers(token),
        json={"symbol": "BTCUSDT", "quantity": "0.10000000"},
    )
    assert buy_resp.status_code == 201

    # Price increases from 50,000 to 60,000 (+20%)
    seed_price("BTCUSDT", "60000.00000000")

    resp = await client.get("/api/portfolio", headers=auth_headers(token))
    assert resp.status_code == 200

    data = resp.json()
    # Cash: 5,000 | Holdings: 0.1 * 60,000 = 6,000 | Total: 11,000
    assert data["cash_balance"] == "5000.00000000"
    assert data["holdings_value"] == "6000.00000000"
    assert data["total_value"] == "11000.00000000"
    assert data["total_pnl"] == "1000.00000000"
    assert data["total_pnl_percent"] == "10.0000"

    h = data["holdings"][0]
    assert h["current_price"] == "60000.00000000"
    assert h["market_value"] == "6000.00000000"
    assert h["cost_basis"] == "5000.00000000"
    assert h["unrealised_pnl"] == "1000.00000000"
    assert h["pnl_percent"] == "20.0000"

