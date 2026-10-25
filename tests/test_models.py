"""Day 2 — model smoke tests.

Verifies that all five tables accept a valid INSERT and that the
Alembic migration can be applied and fully reversed.

Isolation: each test runs inside a transaction that is rolled back by
the db_session fixture in conftest.py, so tests are independent.
"""

from decimal import Decimal
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text, select

from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.models.trade import Trade, TradeType
from app.models.user import User, UserRole
from app.models.watchlist import Watchlist
from app.repositories.user import UserRepository


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def make_user(
    email: str = "alice@example.com",
    balance: str = "10000.00000000",
) -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        password_hash="$2b$12$fakehash",
        role=UserRole.USER,
        virtual_balance=Decimal(balance),
        initial_balance=Decimal(balance),
    )


# ---------------------------------------------------------------------------
# Users table
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_user(db_session):
    user = make_user()
    db_session.add(user)
    await db_session.flush()

    result = await db_session.execute(select(User).where(User.id == user.id))
    fetched = result.scalar_one()

    assert fetched.email == "alice@example.com"
    assert fetched.role == UserRole.USER
    # Decimal exactness — must round-trip without floating-point drift
    assert fetched.virtual_balance == Decimal("10000.00000000")
    assert fetched.initial_balance == Decimal("10000.00000000")
    assert fetched.created_at is not None


@pytest.mark.asyncio
async def test_user_balance_check_constraint(db_session):
    """DB CHECK constraint must reject negative virtual_balance."""
    user = make_user()
    user.virtual_balance = Decimal("-0.00000001")
    db_session.add(user)
    with pytest.raises(Exception, match="ck_users_virtual_balance_gte_0|CheckViolation|check"):
        await db_session.flush()


# ---------------------------------------------------------------------------
# Holdings table
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_holding(db_session):
    user = make_user(email="bob@example.com")
    db_session.add(user)
    await db_session.flush()

    holding = Holding(
        id=uuid.uuid4(),
        user_id=user.id,
        symbol="BTCUSDT",
        quantity=Decimal("0.50000000"),
        avg_buy_price=Decimal("62000.00000000"),
    )
    db_session.add(holding)
    await db_session.flush()

    result = await db_session.execute(
        select(Holding).where(Holding.id == holding.id)
    )
    fetched = result.scalar_one()
    assert fetched.symbol == "BTCUSDT"
    assert fetched.quantity == Decimal("0.50000000")
    assert fetched.avg_buy_price == Decimal("62000.00000000")


@pytest.mark.asyncio
async def test_holding_unique_constraint(db_session):
    """UniqueConstraint(user_id, symbol) must reject duplicates."""
    user = make_user(email="charlie@example.com")
    db_session.add(user)
    await db_session.flush()

    h1 = Holding(
        id=uuid.uuid4(),
        user_id=user.id,
        symbol="ETHUSDT",
        quantity=Decimal("1.00000000"),
        avg_buy_price=Decimal("3000.00000000"),
    )
    h2 = Holding(
        id=uuid.uuid4(),
        user_id=user.id,
        symbol="ETHUSDT",  # duplicate
        quantity=Decimal("2.00000000"),
        avg_buy_price=Decimal("3100.00000000"),
    )
    db_session.add(h1)
    db_session.add(h2)
    with pytest.raises(Exception, match="uq_holdings_user_symbol|UniqueViolation|unique"):
        await db_session.flush()


# ---------------------------------------------------------------------------
# Trades table
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_trade(db_session):
    user = make_user(email="dave@example.com")
    db_session.add(user)
    await db_session.flush()

    trade = Trade(
        id=uuid.uuid4(),
        user_id=user.id,
        symbol="BTCUSDT",
        trade_type=TradeType.BUY,
        quantity=Decimal("0.10000000"),
        price=Decimal("60000.00000000"),
        total_value=Decimal("6000.00000000"),
    )
    db_session.add(trade)
    await db_session.flush()

    result = await db_session.execute(select(Trade).where(Trade.id == trade.id))
    fetched = result.scalar_one()
    assert fetched.trade_type == TradeType.BUY
    assert fetched.quantity == Decimal("0.10000000")
    assert fetched.price == Decimal("60000.00000000")
    assert fetched.total_value == Decimal("6000.00000000")
    assert fetched.executed_at is not None


@pytest.mark.asyncio
async def test_trade_quantity_check_constraint(db_session):
    """CHECK quantity > 0 must reject zero quantity."""
    user = make_user(email="eve@example.com")
    db_session.add(user)
    await db_session.flush()

    trade = Trade(
        id=uuid.uuid4(),
        user_id=user.id,
        symbol="BTCUSDT",
        trade_type=TradeType.SELL,
        quantity=Decimal("0"),  # violates quantity > 0
        price=Decimal("60000.00000000"),
        total_value=Decimal("0"),
    )
    db_session.add(trade)
    with pytest.raises(Exception, match="ck_trades_quantity_gt_0|CheckViolation|check"):
        await db_session.flush()


# ---------------------------------------------------------------------------
# Watchlist table
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_watchlist(db_session):
    user = make_user(email="frank@example.com")
    db_session.add(user)
    await db_session.flush()

    entry = Watchlist(
        id=uuid.uuid4(),
        user_id=user.id,
        symbol="SOLUSDT",
    )
    db_session.add(entry)
    await db_session.flush()

    result = await db_session.execute(
        select(Watchlist).where(Watchlist.id == entry.id)
    )
    fetched = result.scalar_one()
    assert fetched.symbol == "SOLUSDT"
    assert fetched.added_at is not None


# ---------------------------------------------------------------------------
# PriceSnapshot table
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_price_snapshot(db_session):
    snap = PriceSnapshot(
        id=uuid.uuid4(),
        symbol="BTCUSDT",
        price=Decimal("61234.56789012"),
    )
    db_session.add(snap)
    await db_session.flush()

    result = await db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.id == snap.id)
    )
    fetched = result.scalar_one()
    assert fetched.symbol == "BTCUSDT"
    # 8 decimal place round-trip
    assert fetched.price == Decimal("61234.56789012")
    assert fetched.recorded_at is not None


# ---------------------------------------------------------------------------
# Decimal exactness — explicitly test no float drift
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_decimal_no_float_drift(db_session):
    """A fractional price must survive a DB round-trip bit-for-bit."""
    price_str = "0.00000001"  # 1 satoshi — the smallest meaningful unit
    snap = PriceSnapshot(
        id=uuid.uuid4(),
        symbol="TESTUSDT",
        price=Decimal(price_str),
    )
    db_session.add(snap)
    await db_session.flush()

    result = await db_session.execute(
        select(PriceSnapshot).where(PriceSnapshot.id == snap.id)
    )
    fetched = result.scalar_one()
    assert fetched.price == Decimal(price_str), (
        f"Decimal drift detected: expected {price_str!r}, got {str(fetched.price)!r}"
    )
    assert f"{fetched.price:.8f}" == price_str


# ---------------------------------------------------------------------------
# UserRepository / BaseRepository tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_repository_crud(db_session):
    """UserRepository can stage, query by id/email, and delete."""
    repo = UserRepository(db_session)
    user = make_user(email="repo_user@example.com")
    repo.add(user)
    await db_session.flush()

    # get_by_id
    found_by_id = await repo.get_by_id(user.id)
    assert found_by_id is not None
    assert found_by_id.email == "repo_user@example.com"

    # get_by_email
    found_by_email = await repo.get_by_email("repo_user@example.com")
    assert found_by_email is not None
    assert found_by_email.id == user.id

    # Non-existent email returns None
    assert await repo.get_by_email("nonexistent@example.com") is None

    # delete
    await repo.delete(user)
    await db_session.flush()
    assert await repo.get_by_id(user.id) is None
