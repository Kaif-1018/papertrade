"""Tests for TradeService — buy and sell logic with a real test database.

Each test runs inside a rolled-back transaction via the db_session fixture,
so no test data leaks between tests.
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    InsufficientFunds,
    InsufficientHoldings,
    NotFoundError,
    PriceUnavailable,
    StalePrice,
)
from app.core.price_registry import PriceEntry, clear, set_price
from app.models.holding import Holding
from app.models.trade import Trade, TradeType
from app.models.user import User, UserRole
from app.repositories.holding_repository import HoldingRepository
from app.repositories.trade_repository import TradeRepository
from app.repositories.user import UserRepository
from app.schemas.trade import QUANT, quantize
from app.services.trade_service import TradeService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def create_test_user(
    session: AsyncSession,
    balance: str = "10000.00000000",
    email: str | None = None,
) -> User:
    """Insert a test user and commit so it's persisted in the test transaction savepoint."""
    user = User(
        email=email or f"trader-{uuid4().hex[:8]}@test.com",
        password_hash="$2b$12$fakehashfortest",
        role=UserRole.USER,
        virtual_balance=Decimal(balance),
        initial_balance=Decimal(balance),
    )
    session.add(user)
    await session.commit()
    return user


def seed_price(symbol: str, price: str) -> None:
    """Put a fresh price into the registry."""
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
    """Clear the price registry before and after each test."""
    clear()
    yield
    clear()


# ---------------------------------------------------------------------------
# BUY Tests
# ---------------------------------------------------------------------------

class TestBuy:
    """Tests for TradeService.buy()."""

    @pytest.mark.asyncio
    async def test_buy_success(self, db_session: AsyncSession) -> None:
        """A basic buy deducts balance and creates a holding."""
        user = await create_test_user(db_session)
        seed_price("BTCUSDT", "50000.00000000")

        service = TradeService(db_session)
        result = await service.buy(user.id, "BTCUSDT", Decimal("0.1"))

        # Verify trade result.
        assert result.trade.symbol == "BTCUSDT"
        assert result.trade.trade_type == "BUY"
        assert result.trade.quantity == Decimal("0.1")
        assert result.trade.price == Decimal("50000.00000000")

        expected_total = quantize(Decimal("50000") * Decimal("0.1"), ROUND_HALF_UP)
        assert result.trade.total_value == expected_total
        assert result.balance == Decimal("10000") - expected_total

    @pytest.mark.asyncio
    async def test_buy_second_updates_weighted_average(self, db_session: AsyncSession) -> None:
        """A second buy for the same symbol updates avg_buy_price via weighted average."""
        user = await create_test_user(db_session, "100000.00000000")
        seed_price("ETHUSDT", "2000.00000000")

        service = TradeService(db_session)
        await service.buy(user.id, "ETHUSDT", Decimal("1"))

        # Price changes before second buy.
        seed_price("ETHUSDT", "3000.00000000")
        await service.buy(user.id, "ETHUSDT", Decimal("1"))

        # Verify holding: weighted avg = (1*2000 + 1*3000) / 2 = 2500
        result = await db_session.execute(
            select(Holding).where(
                Holding.user_id == user.id,
                Holding.symbol == "ETHUSDT",
            )
        )
        holding = result.scalar_one()
        assert holding.quantity == Decimal("2")
        expected_avg = quantize(
            (Decimal("1") * Decimal("2000") + Decimal("1") * Decimal("3000"))
            / Decimal("2"),
            ROUND_HALF_UP,
        )
        assert holding.avg_buy_price == expected_avg

    @pytest.mark.asyncio
    async def test_buy_insufficient_funds(self, db_session: AsyncSession) -> None:
        """Buy raises InsufficientFunds when balance is too low."""
        user = await create_test_user(db_session, "100.00000000")
        user_id = user.id
        seed_price("BTCUSDT", "50000.00000000")

        service = TradeService(db_session)
        with pytest.raises(InsufficientFunds) as exc_info:
            await service.buy(user_id, "BTCUSDT", Decimal("1"))

        assert exc_info.value.details is not None
        assert "required" in exc_info.value.details
        assert "available" in exc_info.value.details

        # Verify balance unchanged.
        reloaded_user = await UserRepository(db_session).get_by_id(user_id)
        assert reloaded_user is not None
        assert reloaded_user.virtual_balance == Decimal("100.00000000")

    @pytest.mark.asyncio
    async def test_buy_price_unavailable(self, db_session: AsyncSession) -> None:
        """Buy raises PriceUnavailable when symbol has no price entry."""
        user = await create_test_user(db_session)
        user_id = user.id
        # No price seeded for BTCUSDT.

        service = TradeService(db_session)
        with pytest.raises(PriceUnavailable):
            await service.buy(user_id, "BTCUSDT", Decimal("0.1"))

        # Balance unchanged.
        reloaded_user = await UserRepository(db_session).get_by_id(user_id)
        assert reloaded_user is not None
        assert reloaded_user.virtual_balance == Decimal("10000.00000000")

    @pytest.mark.asyncio
    async def test_buy_stale_price(self, db_session: AsyncSession) -> None:
        """Buy raises StalePrice when the cached price is too old."""
        user = await create_test_user(db_session)
        # Seed a price with an old received_at.
        old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        set_price(
            "BTCUSDT",
            Decimal("50000"),
            event_time=old_time,
            clock=lambda: old_time,
        )

        service = TradeService(db_session)
        with pytest.raises(StalePrice):
            await service.buy(user.id, "BTCUSDT", Decimal("0.1"))

    @pytest.mark.asyncio
    async def test_buy_creates_trade_record(self, db_session: AsyncSession) -> None:
        """Buy inserts an immutable trade record."""
        user = await create_test_user(db_session, "100000.00000000")
        seed_price("BTCUSDT", "50000.00000000")

        service = TradeService(db_session)
        result = await service.buy(user.id, "BTCUSDT", Decimal("0.5"))

        # Query trade directly.
        trades = (
            await db_session.execute(
                select(Trade).where(Trade.user_id == user.id)
            )
        ).scalars().all()
        assert len(trades) == 1
        t = trades[0]
        assert t.trade_type == TradeType.BUY
        assert t.symbol == "BTCUSDT"
        assert t.quantity == Decimal("0.5")
        assert t.price == Decimal("50000.00000000")

    @pytest.mark.asyncio
    async def test_buy_precision_tiny_quantity(self, db_session: AsyncSession) -> None:
        """Buy handles very small quantities without precision loss."""
        user = await create_test_user(db_session)
        seed_price("BTCUSDT", "100000.00000000")

        service = TradeService(db_session)
        qty = Decimal("0.00000001")  # 1 satoshi
        result = await service.buy(user.id, "BTCUSDT", qty)

        expected_total = quantize(Decimal("100000") * qty, ROUND_HALF_UP)
        assert result.trade.total_value == expected_total
        assert result.trade.quantity == qty

    @pytest.mark.asyncio
    async def test_buy_user_not_found(self, db_session: AsyncSession) -> None:
        """Buy raises NotFoundError for a non-existent user."""
        seed_price("BTCUSDT", "50000.00000000")
        service = TradeService(db_session)
        with pytest.raises(NotFoundError):
            await service.buy(uuid4(), "BTCUSDT", Decimal("0.1"))


# ---------------------------------------------------------------------------
# SELL Tests
# ---------------------------------------------------------------------------

class TestSell:
    """Tests for TradeService.sell()."""

    @pytest.mark.asyncio
    async def test_sell_partial(self, db_session: AsyncSession) -> None:
        """Sell a portion of holdings — holding remains with reduced quantity."""
        user = await create_test_user(db_session)
        seed_price("ETHUSDT", "2000.00000000")

        service = TradeService(db_session)
        await service.buy(user.id, "ETHUSDT", Decimal("2"))

        # Price rises before selling.
        seed_price("ETHUSDT", "2500.00000000")
        result = await service.sell(user.id, "ETHUSDT", Decimal("1"))

        assert result.trade.trade_type == "SELL"
        assert result.trade.quantity == Decimal("1")
        assert result.trade.price == Decimal("2500.00000000")

        # Holding reduced to 1.
        holding = (
            await db_session.execute(
                select(Holding).where(
                    Holding.user_id == user.id,
                    Holding.symbol == "ETHUSDT",
                )
            )
        ).scalar_one()
        assert holding.quantity == Decimal("1")
        # avg_buy_price unchanged on sell.
        assert holding.avg_buy_price == Decimal("2000.00000000")

    @pytest.mark.asyncio
    async def test_sell_all_deletes_holding(self, db_session: AsyncSession) -> None:
        """Selling entire quantity removes the holding row."""
        user = await create_test_user(db_session, "100000.00000000")
        seed_price("BTCUSDT", "50000.00000000")

        service = TradeService(db_session)
        await service.buy(user.id, "BTCUSDT", Decimal("0.5"))

        seed_price("BTCUSDT", "55000.00000000")
        await service.sell(user.id, "BTCUSDT", Decimal("0.5"))

        # Holding row should be deleted.
        holding = (
            await db_session.execute(
                select(Holding).where(
                    Holding.user_id == user.id,
                    Holding.symbol == "BTCUSDT",
                )
            )
        ).scalar_one_or_none()
        assert holding is None

    @pytest.mark.asyncio
    async def test_sell_insufficient_holdings_no_position(self, db_session: AsyncSession) -> None:
        """Sell raises InsufficientHoldings when no position exists."""
        user = await create_test_user(db_session)
        seed_price("BTCUSDT", "50000.00000000")

        service = TradeService(db_session)
        with pytest.raises(InsufficientHoldings) as exc_info:
            await service.sell(user.id, "BTCUSDT", Decimal("1"))

        assert exc_info.value.details is not None
        assert exc_info.value.details["available"] == "0"

    @pytest.mark.asyncio
    async def test_sell_insufficient_holdings_quantity(self, db_session: AsyncSession) -> None:
        """Sell raises InsufficientHoldings when quantity exceeds position."""
        user = await create_test_user(db_session)
        seed_price("BTCUSDT", "50000.00000000")

        service = TradeService(db_session)
        await service.buy(user.id, "BTCUSDT", Decimal("0.1"))

        with pytest.raises(InsufficientHoldings) as exc_info:
            await service.sell(user.id, "BTCUSDT", Decimal("0.2"))

        assert Decimal(exc_info.value.details["available"]) == Decimal("0.1")

    @pytest.mark.asyncio
    async def test_sell_price_unavailable(self, db_session: AsyncSession) -> None:
        """Sell raises PriceUnavailable if the feed has no price."""
        user = await create_test_user(db_session)
        # Create a holding directly so we have something to sell.
        holding = Holding(
            user_id=user.id,
            symbol="BTCUSDT",
            quantity=Decimal("1"),
            avg_buy_price=Decimal("50000"),
        )
        db_session.add(holding)
        await db_session.flush()

        # No price in registry.
        service = TradeService(db_session)
        with pytest.raises(PriceUnavailable):
            await service.sell(user.id, "BTCUSDT", Decimal("0.5"))

    @pytest.mark.asyncio
    async def test_sell_credits_balance(self, db_session: AsyncSession) -> None:
        """Sell credits the balance with the correct total."""
        user = await create_test_user(db_session, "100000.00000000")
        seed_price("BTCUSDT", "50000.00000000")

        service = TradeService(db_session)
        await service.buy(user.id, "BTCUSDT", Decimal("1"))

        balance_after_buy = Decimal("100000") - quantize(
            Decimal("50000") * Decimal("1"), ROUND_HALF_UP
        )

        seed_price("BTCUSDT", "60000.00000000")
        result = await service.sell(user.id, "BTCUSDT", Decimal("1"))

        sell_total = quantize(Decimal("60000") * Decimal("1"), ROUND_DOWN)
        expected_balance = balance_after_buy + sell_total
        assert result.balance == expected_balance

    @pytest.mark.asyncio
    async def test_sell_creates_trade_record(self, db_session: AsyncSession) -> None:
        """Sell inserts a SELL trade record."""
        user = await create_test_user(db_session, "100000.00000000")
        seed_price("BTCUSDT", "50000.00000000")

        service = TradeService(db_session)
        await service.buy(user.id, "BTCUSDT", Decimal("0.5"))

        seed_price("BTCUSDT", "55000.00000000")
        await service.sell(user.id, "BTCUSDT", Decimal("0.5"))

        trades = (
            await db_session.execute(
                select(Trade).where(Trade.user_id == user.id).order_by(Trade.executed_at)
            )
        ).scalars().all()
        assert len(trades) == 2
        assert trades[0].trade_type == TradeType.BUY
        assert trades[1].trade_type == TradeType.SELL

    @pytest.mark.asyncio
    async def test_rollback_on_trade_insert_failure(self, db_session: AsyncSession) -> None:
        """If inserting the trade record fails, the entire transaction rolls back."""
        user = await create_test_user(db_session, "10000.00000000")
        user_id = user.id
        seed_price("BTCUSDT", "50000.00000000")

        service = TradeService(db_session)

        # Mock TradeRepository.add to raise an error
        def failing_add(trade: Trade) -> None:
            raise RuntimeError("Simulated DB failure on trade insert")

        service._trades.add = failing_add  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="Simulated DB failure"):
            await service.buy(user_id, "BTCUSDT", Decimal("0.1"))

        # Verify balance is unchanged and no holding was created
        reloaded_user = await UserRepository(db_session).get_by_id(user_id)
        assert reloaded_user is not None
        assert reloaded_user.virtual_balance == Decimal("10000.00000000")

        holding = await HoldingRepository(db_session).get_for_update(user_id, "BTCUSDT")
        assert holding is None


# ---------------------------------------------------------------------------
# Rounding / Precision Tests
# ---------------------------------------------------------------------------

class TestPrecision:
    """Verify that Decimal math is exact and never uses float."""

    @pytest.mark.asyncio
    async def test_total_value_is_quantized(self, db_session: AsyncSession) -> None:
        """total_value is quantized to 8 decimal places."""
        user = await create_test_user(db_session)
        # Price that produces a repeating decimal when multiplied by quantity.
        seed_price("BTCUSDT", "33333.33333333")

        service = TradeService(db_session)
        result = await service.buy(user.id, "BTCUSDT", Decimal("0.3"))

        total = result.trade.total_value
        # Verify quantized to 8 dp.
        assert total == total.quantize(QUANT)

    @pytest.mark.asyncio
    async def test_balance_exact_after_buy_sell_cycle(self, db_session: AsyncSession) -> None:
        """After buying and selling at the same price, balance is exactly restored
        (accounting for ROUND_HALF_UP on buy and ROUND_DOWN on sell when they
        happen to produce the same result for round numbers)."""
        user = await create_test_user(db_session)
        seed_price("ETHUSDT", "1000.00000000")

        service = TradeService(db_session)
        await service.buy(user.id, "ETHUSDT", Decimal("1"))

        # Sell at same price.
        result = await service.sell(user.id, "ETHUSDT", Decimal("1"))

        # For round numbers, ROUND_HALF_UP and ROUND_DOWN produce the same total.
        assert result.balance == Decimal("10000.00000000")

