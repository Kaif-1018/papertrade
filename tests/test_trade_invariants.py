"""Property-style invariant testing for trade execution.

Runs pseudo-random sequences of 200 BUY/SELL operations across multiple seeds,
verifying that after EVERY single trade (successful or rejected), the platform's
fundamental accounting invariants hold exactly:
  1. virtual_balance >= 0 (no negative cash balance)
  2. For all holdings: quantity > 0 (no negative or zero holdings)
  3. Conservation of money:
     virtual_balance + sum(BUY totals) - sum(SELL totals) == initial_balance
"""

from decimal import Decimal
import random
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import InsufficientFunds, InsufficientHoldings
from app.models.holding import Holding
from app.models.trade import Trade, TradeType
from app.models.user import User
from app.repositories.holding_repository import HoldingRepository
from app.repositories.user import UserRepository
from app.services.trade_service import TradeService


@pytest.mark.parametrize("seed", [42, 1337, 88888])
@pytest.mark.asyncio
async def test_conservation_of_money_and_non_negative_invariants_200_steps(
    seed: int,
    create_user,
    set_price,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Run 200 random BUY/SELL operations with a fixed seed.

    After every single step:
    - balance >= 0
    - holding quantity > 0 (or row deleted)
    - balance + sum(BUY.total_value) - sum(SELL.total_value) == initial_balance
    """
    rng = random.Random(seed)

    initial_balance = Decimal("10000.00000000")
    user_id = await create_user(balance=initial_balance)

    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    for step in range(200):
        action = rng.choice(["BUY", "SELL"])
        symbol = rng.choice(symbols)
        # Random quantity between 0.0001 and 1.5 with up to 4 decimal places
        qty = Decimal(str(round(rng.uniform(0.0001, 1.5), 4)))
        # Random price between 10.0 and 2000.0 with 2 decimal places
        price = Decimal(str(round(rng.uniform(10.0, 2000.0), 2)))

        set_price(symbol, price)

        # Execute in a dedicated session
        async with session_factory() as session:
            service = TradeService(session)
            try:
                if action == "BUY":
                    await service.buy(user_id, symbol, qty)
                else:
                    await service.sell(user_id, symbol, qty)
            except (InsufficientFunds, InsufficientHoldings):
                # Expected when funds or holdings are exhausted
                pass

        # Invariant checks after each step
        async with session_factory() as session:
            user = await UserRepository(session).get_by_id(user_id)
            assert user is not None, f"User missing at step {step} (seed {seed})"
            assert user.virtual_balance >= Decimal("0"), (
                f"Negative balance at step {step} (seed {seed}): {user.virtual_balance}"
            )

            holdings = (
                await session.execute(
                    select(Holding).where(Holding.user_id == user_id)
                )
            ).scalars().all()
            for h in holdings:
                assert h.quantity > Decimal("0"), (
                    f"Invalid holding qty at step {step} (seed {seed}): {h.symbol} = {h.quantity}"
                )

            trades = (
                await session.execute(
                    select(Trade).where(Trade.user_id == user_id)
                )
            ).scalars().all()

            sum_buys = sum(
                (t.total_value for t in trades if t.trade_type == TradeType.BUY),
                Decimal("0.00000000"),
            )
            sum_sells = sum(
                (t.total_value for t in trades if t.trade_type == TradeType.SELL),
                Decimal("0.00000000"),
            )

            # Invariant: Current Cash + Cash Spent on Buys - Cash Received from Sells == Initial Cash
            expected_cash_accounting = initial_balance
            actual_cash_accounting = user.virtual_balance + sum_buys - sum_sells

            assert actual_cash_accounting == expected_cash_accounting, (
                f"Accounting invariant broken at step {step} (seed {seed}): "
                f"balance={user.virtual_balance}, sum_buys={sum_buys}, sum_sells={sum_sells}, "
                f"diff={actual_cash_accounting - expected_cash_accounting}"
            )

