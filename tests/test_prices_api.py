"""Day 4 — Prices REST API integration tests.

Tests GET /api/prices/{symbol} and GET /api/prices:
- 200 OK for fresh available price (price serialized as string)
- 404 SYMBOL_NOT_SUPPORTED for unknown trading pairs
- 503 PRICE_UNAVAILABLE for supported pairs with no ticks yet
- 503 STALE_PRICE for ticks older than PRICE_STALE_SECONDS
- Consistent error JSON structure: {"error": {"code", "message"}}
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.price_registry import clear, set_price


@pytest.fixture(autouse=True)
def clean_test_registry(monkeypatch):
    """Ensure price feed is disabled in settings and registry is empty."""
    monkeypatch.setenv("ENABLE_PRICE_FEED", "false")
    clear()
    yield
    clear()


@pytest.mark.asyncio
async def test_get_price_success(client):
    """GET /api/prices/BTCUSDT returns 200 with price serialized as string."""
    now = datetime.now(timezone.utc)
    set_price("BTCUSDT", Decimal("67890.12"), now)

    response = await client.get("/api/prices/BTCUSDT")
    assert response.status_code == 200

    data = response.json()
    assert data["symbol"] == "BTCUSDT"
    # Price must be serialized as string with no float artifacts
    assert data["price"] == "67890.12"
    assert "event_time" in data
    assert "age_ms" in data
    assert isinstance(data["age_ms"], int)
    assert data["age_ms"] >= 0


@pytest.mark.asyncio
async def test_get_price_lowercase_symbol_success(client):
    """Lowercase symbol in path is automatically normalized."""
    now = datetime.now(timezone.utc)
    set_price("ETHUSDT", Decimal("3540.50"), now)

    response = await client.get("/api/prices/ethusdt")
    assert response.status_code == 200
    assert response.json()["symbol"] == "ETHUSDT"
    assert response.json()["price"] == "3540.50"


@pytest.mark.asyncio
async def test_get_price_unsupported_symbol(client):
    """Unsupported symbol returns 404 with code SYMBOL_NOT_SUPPORTED."""
    response = await client.get("/api/prices/UNKNOWNCOIN")
    assert response.status_code == 404

    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == "SYMBOL_NOT_SUPPORTED"


@pytest.mark.asyncio
async def test_get_price_unavailable(client):
    """Supported symbol with no ticks received returns 503 PRICE_UNAVAILABLE."""
    # SOLUSDT is in TRADING_PAIRS but has no price set in registry
    response = await client.get("/api/prices/SOLUSDT")
    assert response.status_code == 503

    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == "PRICE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_get_price_stale(client):
    """Price tick received >10s ago returns 503 STALE_PRICE."""
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=20)
    set_price(
        symbol="BTCUSDT",
        price=Decimal("65000.00"),
        event_time=stale_time,
        clock=lambda: stale_time,
    )

    response = await client.get("/api/prices/BTCUSDT")
    assert response.status_code == 503

    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == "STALE_PRICE"


@pytest.mark.asyncio
async def test_get_all_prices(client):
    """GET /api/prices returns list of all current prices in the registry."""
    now = datetime.now(timezone.utc)
    set_price("BTCUSDT", Decimal("67000.00"), now)
    set_price("ETHUSDT", Decimal("3500.00"), now)

    response = await client.get("/api/prices")
    assert response.status_code == 200

    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 2

    symbols = {item["symbol"] for item in data}
    assert symbols == {"BTCUSDT", "ETHUSDT"}

