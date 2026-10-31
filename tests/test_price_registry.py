"""Day 4 — In-memory price registry unit tests.

Verifies O(1) in-memory storage, Decimal exactness without float drift,
uppercase normalization, staleness detection, and error conditions.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.exceptions import PriceUnavailable, StalePrice
from app.core.price_registry import (
    PriceEntry,
    clear,
    get_all,
    get_price,
    is_supported,
    set_price,
)


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure price registry is wiped before and after each test."""
    clear()
    yield
    clear()


def test_set_and_get_price():
    """Setting a price allows retrieving the exact PriceEntry."""
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
    entry = set_price(
        symbol="BTCUSDT",
        price=Decimal("68500.25"),
        event_time=now,
        clock=lambda: now,
    )
    assert entry.symbol == "BTCUSDT"
    assert entry.price == Decimal("68500.25")
    assert entry.event_time == now
    assert entry.received_at == now

    fetched = get_price("BTCUSDT", clock=lambda: now)
    assert fetched == entry


def test_uppercase_normalization():
    """Symbols are automatically normalized to uppercase for storage and retrieval."""
    now = datetime.now(timezone.utc)
    set_price(symbol="ethusdt", price=Decimal("3500.00"), event_time=now)

    # Fetch with uppercase
    fetched = get_price("ETHUSDT")
    assert fetched.symbol == "ETHUSDT"

    # Fetch with mixed case
    fetched_mixed = get_price("EthUsdt")
    assert fetched_mixed.symbol == "ETHUSDT"


def test_never_seen_symbol_raises_price_unavailable():
    """Requesting an unrecorded symbol raises PriceUnavailable."""
    with pytest.raises(PriceUnavailable, match="Price is unavailable"):
        get_price("SOLUSDT")


def test_staleness_detection_with_fake_clock():
    """Price tick older than PRICE_STALE_SECONDS raises StalePrice."""
    t0 = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
    set_price(
        symbol="BTCUSDT",
        price=Decimal("68000.00"),
        event_time=t0,
        clock=lambda: t0,
    )

    # 9 seconds later: still fresh (limit is 10s)
    fresh_time = t0 + timedelta(seconds=9)
    fresh = get_price("BTCUSDT", clock=lambda: fresh_time)
    assert fresh.price == Decimal("68000.00")

    # 11 seconds later: exceeds 10s default threshold -> StalePrice
    stale_time = t0 + timedelta(seconds=11)
    with pytest.raises(StalePrice, match="stale"):
        get_price("BTCUSDT", clock=lambda: stale_time)


def test_decimal_exactness_no_float_artifacts():
    """Decimal values do not suffer from binary float precision drift."""
    # 0.1 cannot be represented exactly in IEEE 754 float
    exact_price = Decimal("0.10000000")
    now = datetime.now(timezone.utc)
    set_price("DOGEUSDT", exact_price, now)

    entry = get_price("DOGEUSDT")
    assert entry.price == exact_price
    # Verify exact string serialization matches
    assert str(entry.price) == "0.10000000"
    assert entry.price != 0.1  # Decimal != float in strict type comparison


def test_is_supported_trading_pairs():
    """Validates symbols against configured TRADING_PAIRS."""
    assert is_supported("BTCUSDT") is True
    assert is_supported("btcusdt") is True
    assert is_supported("ETHUSDT") is True
    assert is_supported("FAKEPAIR") is False


def test_get_all_returns_shallow_copy():
    """get_all returns a dictionary copy that does not mutate internal store."""
    now = datetime.now(timezone.utc)
    set_price("BTCUSDT", Decimal("60000.00"), now)
    set_price("ETHUSDT", Decimal("3000.00"), now)

    all_prices = get_all()
    assert len(all_prices) == 2
    assert "BTCUSDT" in all_prices
    assert "ETHUSDT" in all_prices

    # Modifying the returned dict does not affect internal store
    del all_prices["BTCUSDT"]
    assert "BTCUSDT" in get_all()

