"""Unit and integration tests for WatchlistService."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid

import pytest

from app.core.exceptions import (
    NotInWatchlistError,
    SymbolNotSupported,
    WatchlistFullError,
)
from app.core.price_registry import clear as clear_registry, set_price as reg_set_price
from app.services.watchlist_service import WatchlistService


@pytest.fixture(autouse=True)
def clean_registry():
    clear_registry()
    yield
    clear_registry()


@pytest.mark.asyncio
async def test_add_new_symbol_success(clean_db, session_factory, create_user):
    user_id = await create_user()
    async with session_factory() as session:
        service = WatchlistService(session)
        item, created = await service.add(user_id, "BTCUSDT")

        assert created is True
        assert item.symbol == "BTCUSDT"
        assert item.user_id == user_id
        assert item.added_at is not None


@pytest.mark.asyncio
async def test_add_duplicate_symbol_idempotent(clean_db, session_factory, create_user):
    user_id = await create_user()
    async with session_factory() as session:
        service = WatchlistService(session)
        item1, created1 = await service.add(user_id, "BTCUSDT")
        assert created1 is True

    async with session_factory() as session:
        service = WatchlistService(session)
        item2, created2 = await service.add(user_id, "BTCUSDT")
        assert created2 is False
        assert item2.id == item1.id
        assert item2.symbol == "BTCUSDT"


@pytest.mark.asyncio
async def test_add_lowercase_normalized(clean_db, session_factory, create_user):
    user_id = await create_user()
    async with session_factory() as session:
        service = WatchlistService(session)
        item, created = await service.add(user_id, "  ethusdt  ")
        assert created is True
        assert item.symbol == "ETHUSDT"


@pytest.mark.asyncio
async def test_add_unsupported_symbol_raises(clean_db, session_factory, create_user):
    user_id = await create_user()
    async with session_factory() as session:
        service = WatchlistService(session)
        with pytest.raises(SymbolNotSupported) as exc_info:
            await service.add(user_id, "UNKNOWNCOIN")
        assert exc_info.value.code == "SYMBOL_NOT_SUPPORTED"


@pytest.mark.asyncio
async def test_remove_success(clean_db, session_factory, create_user):
    user_id = await create_user()
    async with session_factory() as session:
        service = WatchlistService(session)
        await service.add(user_id, "SOLUSDT")

    async with session_factory() as session:
        service = WatchlistService(session)
        await service.remove(user_id, "SOLUSDT")
        items = await service.list_with_prices(user_id)
        assert len(items) == 0


@pytest.mark.asyncio
async def test_remove_missing_raises(clean_db, session_factory, create_user):
    user_id = await create_user()
    async with session_factory() as session:
        service = WatchlistService(session)
        with pytest.raises(NotInWatchlistError) as exc_info:
            await service.remove(user_id, "SOLUSDT")
        assert exc_info.value.code == "NOT_IN_WATCHLIST"


@pytest.mark.asyncio
async def test_cap_enforcement(clean_db, session_factory, create_user, monkeypatch):
    monkeypatch.setenv("WATCHLIST_MAX_ITEMS", "3")
    user_id = await create_user()

    async with session_factory() as session:
        service = WatchlistService(session)
        await service.add(user_id, "BTCUSDT")
        await service.add(user_id, "ETHUSDT")
        await service.add(user_id, "SOLUSDT")

        # 4th addition should fail with WatchlistFullError
        with pytest.raises(WatchlistFullError) as exc_info:
            await service.add(user_id, "BNBUSDT")

        assert exc_info.value.code == "WATCHLIST_FULL"
        assert exc_info.value.status_code == 422
        assert exc_info.value.details == {"max": 3}


@pytest.mark.asyncio
async def test_list_with_prices_live_and_stale(clean_db, session_factory, create_user):
    user_id = await create_user()
    now = datetime.now(timezone.utc)

    # 1 fresh price, 1 stale price
    reg_set_price("BTCUSDT", Decimal("65000.50"), now)
    reg_set_price(
        "ETHUSDT",
        Decimal("3500.25"),
        now - timedelta(seconds=20),
        clock=lambda: now - timedelta(seconds=20),
    )

    async with session_factory() as session:
        service = WatchlistService(session)
        await service.add(user_id, "BTCUSDT")
        await service.add(user_id, "ETHUSDT")
        await service.add(user_id, "SOLUSDT")  # no price in registry

        items = await service.list_with_prices(user_id)
        assert len(items) == 3

        # BTCUSDT is fresh
        btc = next(i for i in items if i.symbol == "BTCUSDT")
        assert btc.price == Decimal("65000.50")
        assert btc.stale is False
        assert isinstance(btc.price_age_ms, int)

        # ETHUSDT is stale
        eth = next(i for i in items if i.symbol == "ETHUSDT")
        assert eth.price == Decimal("3500.25")
        assert eth.stale is True
        assert eth.price_age_ms is not None and eth.price_age_ms >= 10000

        # SOLUSDT has empty registry
        sol = next(i for i in items if i.symbol == "SOLUSDT")
        assert sol.price is None
        assert sol.price_age_ms is None
        assert sol.stale is True


@pytest.mark.asyncio
async def test_users_watchlists_isolated(clean_db, session_factory, create_user):
    user_a = await create_user()
    user_b = await create_user()

    async with session_factory() as session:
        service_a = WatchlistService(session)
        await service_a.add(user_a, "BTCUSDT")

    async with session_factory() as session:
        service_b = WatchlistService(session)
        await service_b.add(user_b, "ETHUSDT")

    async with session_factory() as session:
        service = WatchlistService(session)
        items_a = await service.get_watchlist_symbols(user_a)
        items_b = await service.get_watchlist_symbols(user_b)

        assert items_a == ["BTCUSDT"]
        assert items_b == ["ETHUSDT"]

