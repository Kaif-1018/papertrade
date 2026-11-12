"""Concurrency tests for trade execution.

Proves that row-level locking (SELECT ... FOR UPDATE) serializes concurrent
operations on the same user without race conditions, overselling, double spending,
or deadlocks.

Test constraints:
- Real committed transactions across separate sessions (using session_factory).
- Forced overlap via asyncio.Barrier (no arbitrary sleeps).
- Deterministic behavior with no real network.
"""

import asyncio
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import InsufficientFunds, InsufficientHoldings
from app.models.holding import Holding
from app.models.trade import Trade, TradeType
from app.models.user import User
from app.repositories.holding_repository import HoldingRepository
from app.repositories.user import UserRepository
from app.schemas.trade import quantize
from app.services.trade_service import TradeService


@pytest.mark.asyncio
async def test_20_simultaneous_buys_insufficient_funds_boundary(
    create_user,
    set_price,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """20 simultaneous BUYs of 1 unit at price 100 for a user with 550 balance.

    Assert:
    - Exactly 5 succeed.
    - Exactly 15 fail with InsufficientFunds.
    - Final balance is exactly 50.
    - Holding quantity is exactly 5.
    - Exactly 5 trade rows exist.
    """
    user_id = await create_user(balance="550.00000000")
    set_price("BTCUSDT", "100.00000000")

    num_tasks = 20
    barrier = asyncio.Barrier(num_tasks)

    async def buy_task() -> str:
        await barrier.wait()
        async with session_factory() as session:
            service = TradeService(session)
            try:
                await service.buy(user_id, "BTCUSDT", Decimal("1"))
                return "SUCCESS"
            except InsufficientFunds:
                return "INSUFFICIENT_FUNDS"

    results = await asyncio.gather(*(buy_task() for _ in range(num_tasks)))

    successes = [r for r in results if r == "SUCCESS"]
    failures = [r for r in results if r == "INSUFFICIENT_FUNDS"]

    assert len(successes) == 5, f"Expected 5 successes, got {len(successes)}"
    assert len(failures) == 15, f"Expected 15 failures, got {len(failures)}"

    # Check database state across fresh session
    async with session_factory() as session:
        user = await UserRepository(session).get_by_id(user_id)
        assert user is not None
        assert user.virtual_balance == Decimal("50.00000000")

        holding = await HoldingRepository(session).get_for_update(user_id, "BTCUSDT")
        assert holding is not None
        assert holding.quantity == Decimal("5.00000000")
        assert holding.avg_buy_price == Decimal("100.00000000")

        trades = (
            await session.execute(
                select(Trade).where(Trade.user_id == user_id)
            )
        ).scalars().all()
        assert len(trades) == 5
        assert all(t.trade_type == TradeType.BUY for t in trades)


@pytest.mark.asyncio
async def test_20_simultaneous_sells_insufficient_holdings_boundary(
    create_user,
    set_price,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """20 simultaneous SELLs of 1 unit against a holding of 7.

    Assert:
    - Exactly 7 succeed.
    - Exactly 13 fail with InsufficientHoldings.
    - The holding row is deleted upon reaching 0.
    - Balance is credited exactly 7 times (7 * 100 = 700).
    - Exactly 7 trade rows exist.
    """
    user_id = await create_user(balance="0.00000000")
    set_price("BTCUSDT", "100.00000000")

    # Seed initial holding of 7 units
    async with session_factory() as session:
        HoldingRepository(session).create(
            user_id=user_id,
            symbol="BTCUSDT",
            quantity=Decimal("7.00000000"),
            avg_buy_price=Decimal("100.00000000"),
        )
        await session.commit()

    num_tasks = 20
    barrier = asyncio.Barrier(num_tasks)

    async def sell_task() -> str:
        await barrier.wait()
        async with session_factory() as session:
            service = TradeService(session)
            try:
                await service.sell(user_id, "BTCUSDT", Decimal("1"))
                return "SUCCESS"
            except InsufficientHoldings:
                return "INSUFFICIENT_HOLDINGS"

    results = await asyncio.gather(*(sell_task() for _ in range(num_tasks)))

    successes = [r for r in results if r == "SUCCESS"]
    failures = [r for r in results if r == "INSUFFICIENT_HOLDINGS"]

    assert len(successes) == 7, f"Expected 7 successes, got {len(successes)}"
    assert len(failures) == 13, f"Expected 13 failures, got {len(failures)}"

    async with session_factory() as session:
        user = await UserRepository(session).get_by_id(user_id)
        assert user is not None
        assert user.virtual_balance == Decimal("700.00000000")

        holding = await HoldingRepository(session).get_for_update(user_id, "BTCUSDT")
        assert holding is None, "Holding row must be deleted when quantity reaches 0"

        trades = (
            await session.execute(
                select(Trade).where(Trade.user_id == user_id)
            )
        ).scalars().all()
        assert len(trades) == 7
        assert all(t.trade_type == TradeType.SELL for t in trades)


@pytest.mark.asyncio
async def test_mixed_buys_and_sells_replay_consistency(
    create_user,
    set_price,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mixed BUYs and SELLs on the same user at once.

    Assert:
    - Final state in DB equals replaying the successful trades in executed_at order.
    """
    initial_balance = Decimal("1000.00000000")
    initial_holding = Decimal("5.00000000")
    user_id = await create_user(balance=initial_balance)
    set_price("ETHUSDT", "200.00000000")

    # Seed initial holding
    async with session_factory() as session:
        HoldingRepository(session).create(
            user_id=user_id,
            symbol="ETHUSDT",
            quantity=initial_holding,
            avg_buy_price=Decimal("200.00000000"),
        )
        await session.commit()

    # 10 BUYs of 1 ETH and 10 SELLs of 1 ETH concurrently
    orders = [("BUY", Decimal("1"))] * 10 + [("SELL", Decimal("1"))] * 10
    num_tasks = len(orders)
    barrier = asyncio.Barrier(num_tasks)

    async def execute_order(action: str, qty: Decimal) -> None:
        await barrier.wait()
        async with session_factory() as session:
            service = TradeService(session)
            try:
                if action == "BUY":
                    await service.buy(user_id, "ETHUSDT", qty)
                else:
                    await service.sell(user_id, "ETHUSDT", qty)
            except (InsufficientFunds, InsufficientHoldings):
                pass

    await asyncio.gather(*(execute_order(action, qty) for action, qty in orders))

    # Read the final DB state and all executed trades sorted chronologically
    async with session_factory() as session:
        user = await UserRepository(session).get_by_id(user_id)
        assert user is not None
        final_db_balance = user.virtual_balance

        holding = await HoldingRepository(session).get_for_update(user_id, "ETHUSDT")
        final_db_holding_qty = holding.quantity if holding else Decimal("0")

        trades = (
            await session.execute(
                select(Trade)
                .where(Trade.user_id == user_id)
                .order_by(Trade.executed_at.asc(), Trade.id.asc())
            )
        ).scalars().all()

    # Replay all committed trades in chronological order
    replayed_balance = initial_balance
    replayed_holding = initial_holding

    for trade in trades:
        if trade.trade_type == TradeType.BUY:
            replayed_balance -= trade.total_value
            replayed_holding += trade.quantity
        elif trade.trade_type == TradeType.SELL:
            replayed_balance += trade.total_value
            replayed_holding -= trade.quantity

    assert final_db_balance == replayed_balance, (
        f"DB balance {final_db_balance} != replayed balance {replayed_balance}"
    )
    assert final_db_holding_qty == replayed_holding, (
        f"DB holding {final_db_holding_qty} != replayed holding {replayed_holding}"
    )


@pytest.mark.asyncio
async def test_two_different_users_trading_concurrently_do_not_block(
    create_user,
    set_price,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two different users trading concurrently must not block or affect each other."""
    user_a = await create_user(balance="1000.00000000")
    user_b = await create_user(balance="2000.00000000")
    set_price("BTCUSDT", "500.00000000")

    barrier = asyncio.Barrier(2)

    async def trade_user(u_id: UUID, qty: Decimal) -> None:
        await barrier.wait()
        async with session_factory() as session:
            service = TradeService(session)
            await service.buy(u_id, "BTCUSDT", qty)

    # User A buys 1 BTC (cost 500), User B buys 3 BTC (cost 1500)
    await asyncio.gather(
        trade_user(user_a, Decimal("1.00000000")),
        trade_user(user_b, Decimal("3.00000000")),
    )

    async with session_factory() as session:
        db_user_a = await UserRepository(session).get_by_id(user_a)
        db_user_b = await UserRepository(session).get_by_id(user_b)
        assert db_user_a.virtual_balance == Decimal("500.00000000")
        assert db_user_b.virtual_balance == Decimal("500.00000000")

        holding_a = await HoldingRepository(session).get_for_update(user_a, "BTCUSDT")
        holding_b = await HoldingRepository(session).get_for_update(user_b, "BTCUSDT")
        assert holding_a.quantity == Decimal("1.00000000")
        assert holding_b.quantity == Decimal("3.00000000")


@pytest.mark.asyncio
async def test_deadlock_check_50_mixed_trades_across_3_users_and_2_symbols(
    create_user,
    set_price,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deadlock check: run 50 mixed trades across 3 users and 2 symbols concurrently with a timeout of 10s.

    No deadlock errors (e.g. 40P01 deadlock_detected) allowed.
    """
    user_ids = [
        await create_user(balance="50000.00000000", email=f"deadlock-u{i}@test.com")
        for i in range(3)
    ]
    set_price("BTCUSDT", "50000.00000000")
    set_price("ETHUSDT", "3000.00000000")

    # Give each user some initial holdings in both symbols so sells can succeed
    async with session_factory() as session:
        for u_id in user_ids:
            HoldingRepository(session).create(
                user_id=u_id,
                symbol="BTCUSDT",
                quantity=Decimal("10.00000000"),
                avg_buy_price=Decimal("50000.00000000"),
            )
            HoldingRepository(session).create(
                user_id=u_id,
                symbol="ETHUSDT",
                quantity=Decimal("50.00000000"),
                avg_buy_price=Decimal("3000.00000000"),
            )
        await session.commit()

    symbols = ["BTCUSDT", "ETHUSDT"]
    actions = ["BUY", "SELL"]

    # Build 50 trade specs distributed across the 3 users and 2 symbols
    tasks_spec = []
    for i in range(50):
        u = user_ids[i % 3]
        s = symbols[i % 2]
        act = actions[(i // 2) % 2]
        qty = Decimal("0.10000000") if s == "BTCUSDT" else Decimal("1.00000000")
        tasks_spec.append((u, s, act, qty))

    barrier = asyncio.Barrier(len(tasks_spec))

    async def execute_trade(u_id: UUID, sym: str, action: str, qty: Decimal) -> None:
        await barrier.wait()
        async with session_factory() as session:
            service = TradeService(session)
            try:
                if action == "BUY":
                    await service.buy(u_id, sym, qty)
                else:
                    await service.sell(u_id, sym, qty)
            except (InsufficientFunds, InsufficientHoldings):
                pass

    # Must complete within 10 seconds without deadlocks
    await asyncio.wait_for(
        asyncio.gather(
            *(execute_trade(u, s, act, q) for u, s, act, q in tasks_spec)
        ),
        timeout=10.0,
    )

    # Verify each user has a valid state and trades were executed
    async with session_factory() as session:
        total_trades = 0
        for u_id in user_ids:
            user = await UserRepository(session).get_by_id(u_id)
            assert user.virtual_balance >= Decimal("0")
            trades = (
                await session.execute(
                    select(Trade).where(Trade.user_id == u_id)
                )
            ).scalars().all()
            total_trades += len(trades)
        assert total_trades > 0
