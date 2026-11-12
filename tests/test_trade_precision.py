"""Precision tests for trade execution.

Verifies zero floating-point drift, exact Decimal math, quantize behavior,
weighted-average price calculation, and rejection of quantities > 8 decimals.
"""

from decimal import Decimal
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import InvalidQuantity
from app.models.holding import Holding
from app.models.trade import Trade
from app.models.user import User
from app.repositories.holding_repository import HoldingRepository
from app.repositories.user import UserRepository
from app.schemas.trade import QUANT, TradeRequest
from app.services.trade_service import TradeService


@pytest.mark.asyncio
async def test_tiny_quantity_precision(
    create_user,
    set_price,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tiny quantity: buy Decimal('0.00000001') at 67123.45678901.

    Hand calculation:
      quantity = Decimal("0.00000001") (10^-8)
      price    = Decimal("67123.45678901")
      raw product = 67123.45678901 * 10^-8 = 0.0006712345678901
      quantize(0.0006712345678901, ROUND_HALF_UP) to 8 decimal places:
        Decimal places 1 through 8: 0.00067123
        The 9th decimal digit is '4'.
        Since 4 < 5, ROUND_HALF_UP rounds DOWN without incrementing the 8th digit.
      exact stored total_value = Decimal("0.00067123")
    """
    user_id = await create_user(balance="100.00000000")
    set_price("BTCUSDT", "67123.45678901")

    qty = Decimal("0.00000001")
    async with session_factory() as session:
        service = TradeService(session)
        result = await service.buy(user_id, "BTCUSDT", qty)

    # Hand-computed expectation:
    expected_total = Decimal("0.00067123")
    assert result.trade.total_value == expected_total
    assert result.trade.quantity == qty
    assert result.balance == Decimal("100.00000000") - expected_total

    # Verify directly in DB
    async with session_factory() as session:
        trade = (
            await session.execute(
                select(Trade).where(Trade.user_id == user_id)
            )
        ).scalar_one()
        assert trade.total_value == expected_total


@pytest.mark.asyncio
async def test_1000_sequential_buys_zero_drift_vs_float(
    create_user,
    set_price,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """1000 sequential buys of 0.1 at 0.1 price.

    With exact Decimal arithmetic:
      Each buy total = quantize(Decimal("0.1") * Decimal("0.1")) = Decimal("0.01000000").
      1000 buys total cost = 1000 * 0.01000000 = Decimal("10.00000000").
      Initial balance: 100.00000000
      Expected final balance: Decimal("90.00000000") exactly.
      Expected final holding: 1000 * Decimal("0.1") = Decimal("100.00000000") exactly.

    Float drift comparison:
      In binary float: 0.1 is represented as 0.1000000000000000055511151231257827...
      0.1 * 0.1 = 0.010000000000000002
      Accumulating 1000 iterations in float yields drift:
      sum(0.1 for _ in range(1000)) != 100.0 (typically 99.99999999999999 or accumulates drift).
      In Decimal, there is strictly zero drift.
    """
    initial_balance = Decimal("100.00000000")
    user_id = await create_user(balance=initial_balance)
    set_price("DOGEUSDT", "0.10000000")

    buy_qty = Decimal("0.10000000")

    # Float comparison accumulator:
    float_bal = float(initial_balance)
    float_qty = 0.0
    for _ in range(1000):
        float_cost = 0.1 * 0.1
        float_bal -= float_cost
        float_qty += 0.1

    # Perform 1000 sequential buys in Decimal via TradeService.
    # Refresh price periodically as a real ticker feed does, so it does not become stale (>10s)
    # during 1000 DB transactions.
    async with session_factory() as session:
        service = TradeService(session)
        for i in range(1000):
            if i % 10 == 0:
                set_price("DOGEUSDT", "0.10000000")
            await service.buy(user_id, "DOGEUSDT", buy_qty)

    async with session_factory() as session:
        user = await UserRepository(session).get_by_id(user_id)
        holding = await HoldingRepository(session).get_for_update(user_id, "DOGEUSDT")

        # Decimal exact values:
        expected_balance = Decimal("90.00000000")
        expected_holding = Decimal("100.00000000")

        assert user.virtual_balance == expected_balance, (
            f"Expected {expected_balance}, got {user.virtual_balance}"
        )
        assert holding.quantity == expected_holding, (
            f"Expected {expected_holding}, got {holding.quantity}"
        )

        # Confirm float would drift:
        # float_qty is 99.99999999999999, NOT 100.0!
        assert float_qty != 100.0 or float_bal != 90.0


@pytest.mark.asyncio
async def test_weighted_average_price_after_5_buys(
    create_user,
    set_price,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Weighted average price after 5 buys at different prices.

    Independent Hand Calculation:
      Buy 1: 2.00000000 @ 100.00000000
             qty = 2, total_cost = 200, avg = 100.00000000
      Buy 2: 3.00000000 @ 150.00000000
             qty = 5, total_cost = 200 + 450 = 650, avg = 650/5 = 130.00000000
      Buy 3: 5.00000000 @ 200.00000000
             qty = 10, total_cost = 650 + 1000 = 1650, avg = 1650/10 = 165.00000000
      Buy 4: 10.00000000 @ 180.00000000
             qty = 20, total_cost = 1650 + 1800 = 3450, avg = 3450/20 = 172.50000000
      Buy 5: 4.00000000 @ 250.00000000
             qty = 24, total_cost = 3450 + 1000 = 4450
             avg = 4450 / 24 = 185.41666666666666666...
             quantized with ROUND_HALF_UP to 8 decimals:
               The 8th decimal digit is 6, the 9th digit is 6 (>= 5).
               ROUND_HALF_UP rounds up: 185.41666667
      Expected final avg_buy_price = Decimal("185.41666667")
      Expected final quantity      = Decimal("24.00000000")
    """
    user_id = await create_user(balance="100000.00000000")

    buys = [
        (Decimal("2.00000000"), Decimal("100.00000000")),
        (Decimal("3.00000000"), Decimal("150.00000000")),
        (Decimal("5.00000000"), Decimal("200.00000000")),
        (Decimal("10.00000000"), Decimal("180.00000000")),
        (Decimal("4.00000000"), Decimal("250.00000000")),
    ]

    async with session_factory() as session:
        service = TradeService(session)
        for qty, price in buys:
            set_price("SOLUSDT", price)
            await service.buy(user_id, "SOLUSDT", qty)

    async with session_factory() as session:
        holding = await HoldingRepository(session).get_for_update(user_id, "SOLUSDT")
        assert holding is not None
        assert holding.quantity == Decimal("24.00000000")
        assert holding.avg_buy_price == Decimal("185.41666667")


@pytest.mark.asyncio
async def test_buy_then_sell_same_quantity_and_price_exact_restoration(
    create_user,
    set_price,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Buy then sell the same quantity at the same price: balance returns to exactly the starting value."""
    initial_balance = Decimal("50000.00000000")
    user_id = await create_user(balance=initial_balance)
    set_price("ETHUSDT", "3125.55000000")

    qty = Decimal("2.50000000")

    async with session_factory() as session:
        service = TradeService(session)
        # Buy
        buy_res = await service.buy(user_id, "ETHUSDT", qty)
        assert buy_res.balance < initial_balance

        # Sell at identical price
        sell_res = await service.sell(user_id, "ETHUSDT", qty)
        assert sell_res.balance == initial_balance

    # Verify directly from DB
    async with session_factory() as session:
        user = await UserRepository(session).get_by_id(user_id)
        assert user.virtual_balance == initial_balance

        holding = await HoldingRepository(session).get_for_update(user_id, "ETHUSDT")
        assert holding is None


@pytest.mark.asyncio
async def test_reject_quantities_with_more_than_8_decimals_writes_nothing(
    create_user,
    set_price,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reject quantities with more than 8 decimals and verify nothing is written."""
    initial_balance = Decimal("10000.00000000")
    user_id = await create_user(balance=initial_balance)
    set_price("BTCUSDT", "50000.00000000")

    invalid_qty = Decimal("0.123456789")  # 9 decimal places

    async with session_factory() as session:
        service = TradeService(session)
        with pytest.raises(InvalidQuantity, match="at most 8 decimal places"):
            await service.buy(user_id, "BTCUSDT", invalid_qty)

        with pytest.raises(InvalidQuantity, match="at most 8 decimal places"):
            await service.sell(user_id, "BTCUSDT", invalid_qty)

    # Verify nothing was written to users, holdings, or trades
    async with session_factory() as session:
        user = await UserRepository(session).get_by_id(user_id)
        assert user.virtual_balance == initial_balance

        holding = await HoldingRepository(session).get_for_update(user_id, "BTCUSDT")
        assert holding is None

        trades = (
            await session.execute(
                select(Trade).where(Trade.user_id == user_id)
            )
        ).scalars().all()
        assert len(trades) == 0

