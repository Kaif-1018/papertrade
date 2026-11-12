"""API integration tests for trade endpoints.

Tests the full HTTP flow: auth → trade → response, using the
httpx AsyncClient with the test FastAPI app.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.price_registry import clear, set_price


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def register_and_login(client: AsyncClient) -> tuple[str, dict]:
    """Register a new user and return (access_token, user_info)."""
    import uuid

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_registry():
    clear()
    yield
    clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTradeAPI:
    """Integration tests for POST /api/trade/buy and /api/trade/sell."""

    @pytest.mark.asyncio
    async def test_buy_success(self, client: AsyncClient) -> None:
        """Full buy flow returns 201 with trade details."""
        token, _ = await register_and_login(client)
        seed_price("BTCUSDT", "50000.00000000")

        resp = await client.post(
            "/api/trade/buy",
            json={"symbol": "BTCUSDT", "quantity": "0.1"},
            headers=auth_headers(token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "trade" in body
        assert "balance" in body
        assert body["trade"]["symbol"] == "BTCUSDT"
        assert body["trade"]["trade_type"] == "BUY"
        assert body["trade"]["quantity"] == "0.1"

    @pytest.mark.asyncio
    async def test_sell_success(self, client: AsyncClient) -> None:
        """Buy then sell returns 201 with updated balance."""
        token, _ = await register_and_login(client)
        seed_price("ETHUSDT", "2000.00000000")

        # Buy first.
        buy_resp = await client.post(
            "/api/trade/buy",
            json={"symbol": "ETHUSDT", "quantity": "1"},
            headers=auth_headers(token),
        )
        assert buy_resp.status_code == 201

        # Sell.
        seed_price("ETHUSDT", "2500.00000000")
        sell_resp = await client.post(
            "/api/trade/sell",
            json={"symbol": "ETHUSDT", "quantity": "0.5"},
            headers=auth_headers(token),
        )
        assert sell_resp.status_code == 201
        body = sell_resp.json()
        assert body["trade"]["trade_type"] == "SELL"
        assert body["trade"]["quantity"] == "0.5"

    @pytest.mark.asyncio
    async def test_buy_requires_auth(self, client: AsyncClient) -> None:
        """Buy without a token returns 401."""
        seed_price("BTCUSDT", "50000.00000000")

        resp = await client.post(
            "/api/trade/buy",
            json={"symbol": "BTCUSDT", "quantity": "0.1"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_sell_requires_auth(self, client: AsyncClient) -> None:
        """Sell without a token returns 401."""
        seed_price("BTCUSDT", "50000.00000000")

        resp = await client.post(
            "/api/trade/sell",
            json={"symbol": "BTCUSDT", "quantity": "0.1"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_buy_insufficient_funds_error(self, client: AsyncClient) -> None:
        """Buy with insufficient balance returns 422 with details."""
        token, _ = await register_and_login(client)
        seed_price("BTCUSDT", "50000.00000000")

        resp = await client.post(
            "/api/trade/buy",
            json={"symbol": "BTCUSDT", "quantity": "1000"},
            headers=auth_headers(token),
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "INSUFFICIENT_FUNDS"
        assert "details" in body["error"]
        assert "required" in body["error"]["details"]
        assert "available" in body["error"]["details"]

    @pytest.mark.asyncio
    async def test_sell_insufficient_holdings_error(self, client: AsyncClient) -> None:
        """Sell without holdings returns 422 with details."""
        token, _ = await register_and_login(client)
        seed_price("BTCUSDT", "50000.00000000")

        resp = await client.post(
            "/api/trade/sell",
            json={"symbol": "BTCUSDT", "quantity": "1"},
            headers=auth_headers(token),
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"]["code"] == "INSUFFICIENT_HOLDINGS"
        assert "details" in body["error"]

    @pytest.mark.asyncio
    async def test_buy_price_unavailable_error(self, client: AsyncClient) -> None:
        """Buy when no price in registry returns 503."""
        token, _ = await register_and_login(client)
        # No price seeded.

        resp = await client.post(
            "/api/trade/buy",
            json={"symbol": "BTCUSDT", "quantity": "0.1"},
            headers=auth_headers(token),
        )
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "PRICE_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_buy_unsupported_symbol_error(self, client: AsyncClient) -> None:
        """Buy with an unsupported symbol returns 422."""
        token, _ = await register_and_login(client)

        resp = await client.post(
            "/api/trade/buy",
            json={"symbol": "FAKEUSDT", "quantity": "1"},
            headers=auth_headers(token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_buy_invalid_quantity_zero(self, client: AsyncClient) -> None:
        """Buy with quantity=0 returns 422."""
        token, _ = await register_and_login(client)

        resp = await client.post(
            "/api/trade/buy",
            json={"symbol": "BTCUSDT", "quantity": "0"},
            headers=auth_headers(token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_buy_invalid_quantity_negative(self, client: AsyncClient) -> None:
        """Buy with negative quantity returns 422."""
        token, _ = await register_and_login(client)

        resp = await client.post(
            "/api/trade/buy",
            json={"symbol": "BTCUSDT", "quantity": "-1"},
            headers=auth_headers(token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_buy_invalid_quantity_too_many_decimals(self, client: AsyncClient) -> None:
        """Buy with >8 decimal places returns 422."""
        token, _ = await register_and_login(client)

        resp = await client.post(
            "/api/trade/buy",
            json={"symbol": "BTCUSDT", "quantity": "0.123456789"},
            headers=auth_headers(token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_buy_quantity_as_number(self, client: AsyncClient) -> None:
        """Buy accepts quantity as a JSON number (not just string)."""
        token, _ = await register_and_login(client)
        seed_price("BTCUSDT", "50000.00000000")

        resp = await client.post(
            "/api/trade/buy",
            json={"symbol": "BTCUSDT", "quantity": 0.1},
            headers=auth_headers(token),
        )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_decimals_serialized_as_strings(self, client: AsyncClient) -> None:
        """All decimal fields in the response are JSON strings."""
        token, _ = await register_and_login(client)
        seed_price("BTCUSDT", "50000.00000000")

        resp = await client.post(
            "/api/trade/buy",
            json={"symbol": "BTCUSDT", "quantity": "0.1"},
            headers=auth_headers(token),
        )
        body = resp.json()
        assert isinstance(body["trade"]["quantity"], str)
        assert isinstance(body["trade"]["price"], str)
        assert isinstance(body["trade"]["total_value"], str)
        assert isinstance(body["balance"], str)

    @pytest.mark.asyncio
    async def test_full_flow_buy_sell(self, client: AsyncClient) -> None:
        """Full flow: register, login, buy, sell — balances change correctly."""
        token, _ = await register_and_login(client)
        seed_price("ETHUSDT", "1000.00000000")

        # Buy 2 ETH.
        buy_resp = await client.post(
            "/api/trade/buy",
            json={"symbol": "ETHUSDT", "quantity": "2"},
            headers=auth_headers(token),
        )
        assert buy_resp.status_code == 201
        buy_balance = Decimal(buy_resp.json()["balance"])
        assert buy_balance == Decimal("8000.00000000")

        # Sell 1 ETH at a higher price.
        seed_price("ETHUSDT", "1500.00000000")
        sell_resp = await client.post(
            "/api/trade/sell",
            json={"symbol": "ETHUSDT", "quantity": "1"},
            headers=auth_headers(token),
        )
        assert sell_resp.status_code == 201
        sell_balance = Decimal(sell_resp.json()["balance"])
        assert sell_balance == Decimal("9500.00000000")

    @pytest.mark.asyncio
    async def test_buy_quantity_exceeds_max(self, client: AsyncClient) -> None:
        """Buy with quantity > 1_000_000 returns 422."""
        token, _ = await register_and_login(client)

        resp = await client.post(
            "/api/trade/buy",
            json={"symbol": "BTCUSDT", "quantity": "1000001"},
            headers=auth_headers(token),
        )
        assert resp.status_code == 422

