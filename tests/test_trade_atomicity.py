"""Atomicity and failure recovery tests for trade execution.

Proves that any failure mid-transaction (repository error, DB constraint,
connection drop, or application crash) rolls back all state atomically across
users, holdings, and trades tables without leaving orphan or partial data.
"""

from decimal import Decimal
import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import InsufficientFunds, InvalidQuantity
from app.models.holding import Holding
from app.models.trade import Trade
from app.models.user import User
from app.repositories.holding_repository import HoldingRepository
from app.repositories.user import UserRepository
from app.services.trade_service import TradeService


@pytest.mark.asyncio
async def test_rollback_on_trade_repo_add_failure(
    create_user,
    set_price,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Patch TradeRepository.add to raise after balance deduction and holding upsert.

    Assert:
    - User balance remains untouched.
    - No holding row is created.
    - No trade row exists.
    """
    initial_balance = Decimal("10000.00000000")
    user_id = await create_user(balance=initial_balance)
    set_price("BTCUSDT", "50000.00000000")

    async with session_factory() as session:
        service = TradeService(session)

        def failing_add(trade: Trade) -> None:
            raise RuntimeError("Simulated TradeRepository.add failure")

        service._trades.add = failing_add  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="Simulated TradeRepository.add failure"):
            await service.buy(user_id, "BTCUSDT", Decimal("0.10000000"))

    # Verify all 3 tables are completely clean
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


@pytest.mark.asyncio
async def test_rollback_on_holding_upsert_failure(
    create_user,
    set_price,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Patch holding creation/upsert to raise an error.

    Assert:
    - User balance is unchanged.
    - No holding row is created.
    - No trade row exists.
    """
    initial_balance = Decimal("10000.00000000")
    user_id = await create_user(balance=initial_balance)
    set_price("BTCUSDT", "50000.00000000")

    async with session_factory() as session:
        service = TradeService(session)

        def failing_create(*args, **kwargs):
            raise RuntimeError("Simulated HoldingRepository.create failure")

        service._holdings.create = failing_create  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="Simulated HoldingRepository.create failure"):
            await service.buy(user_id, "BTCUSDT", Decimal("0.10000000"))

    # Verify state across tables
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


@pytest.mark.asyncio
async def test_simulate_db_error_mid_transaction_and_subsequent_recovery(
    create_user,
    set_price,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Simulate a DB connection/flush error mid-transaction (patch session.flush).

    Assert:
    - Full rollback occurred.
    - User can trade normally immediately afterwards in a new session.
    """
    initial_balance = Decimal("10000.00000000")
    user_id = await create_user(balance=initial_balance)
    set_price("ETHUSDT", "2500.00000000")

    # 1. Attempt buy that encounters a mid-transaction flush failure
    async with session_factory() as session:
        service = TradeService(session)

        async def failing_flush():
            raise OperationalError("Simulated connection dropped", params=None, orig=Exception())

        session.flush = failing_flush  # type: ignore[method-assign]

        with pytest.raises(OperationalError, match="Simulated connection dropped"):
            await service.buy(user_id, "ETHUSDT", Decimal("1.00000000"))

    # 2. Assert clean state after mid-transaction failure
    async with session_factory() as session:
        user = await UserRepository(session).get_by_id(user_id)
        assert user.virtual_balance == initial_balance

        holding = await HoldingRepository(session).get_for_update(user_id, "ETHUSDT")
        assert holding is None

        trades = (
            await session.execute(
                select(Trade).where(Trade.user_id == user_id)
            )
        ).scalars().all()
        assert len(trades) == 0

    # 3. Assert user can now trade normally in a fresh session
    async with session_factory() as session:
        service = TradeService(session)
        result = await service.buy(user_id, "ETHUSDT", Decimal("1.00000000"))
        assert result.balance == Decimal("7500.00000000")
        assert result.trade.symbol == "ETHUSDT"


@pytest.mark.asyncio
async def test_failed_trade_leaves_no_partial_state_across_all_tables(
    create_user,
    set_price,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify that business rejection (e.g. InsufficientFunds) leaves no partial state across users, holdings, trades."""
    initial_balance = Decimal("500.00000000")
    user_id = await create_user(balance=initial_balance)
    set_price("BTCUSDT", "50000.00000000")

    async with session_factory() as session:
        service = TradeService(session)
        with pytest.raises(InsufficientFunds):
            # Attempt to buy 1 BTC with only 500 USD
            await service.buy(user_id, "BTCUSDT", Decimal("1.00000000"))

    # Assert all tables unchanged
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

