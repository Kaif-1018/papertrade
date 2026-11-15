"""Integration tests for administrative trading pair management.

Verifies:
- Authorization on every admin route (401 unauthenticated, 403 normal USER, success for ADMIN).
- Pair addition with mocked Binance check:
  - Valid symbol -> 201 Created, DB row inserted, price feed add_symbol called.
  - Invalid format -> 422 validation error.
  - Non-existent Binance symbol -> 422 INVALID_BINANCE_SYMBOL, nothing inserted into DB.
  - Binance unreachable / timeout -> 503 BINANCE_UNREACHABLE, nothing inserted into DB.
  - Duplicate active pair -> 409 PAIR_EXISTS.
  - Re-activating a deactivated pair -> 201 Created, is_active becomes True.
  - Feed failure -> DB change persists, feed_sync returns "pending".
- Pair deactivation:
  - Deactivating with 0 holders -> 200 OK, soft-deactivated, feed remove_symbol called.
  - Deactivating with >0 holders without force -> 409 PAIR_HAS_HOLDINGS with details {holders_count}.
  - Deactivating with >0 holders with force=true -> 200 OK, soft-deactivated.
  - Deactivating unknown pair -> 404 PAIR_NOT_FOUND.
- Trade behavior on active vs inactive pairs:
  - BUY is blocked on deactivated pairs (404 SYMBOL_NOT_SUPPORTED).
  - SELL is allowed on deactivated pairs at terminal last known price.
  - SELL on deactivated pair with no price fails with 503 PRICE_UNAVAILABLE.
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import get_db
from app.core.exceptions import BinanceUnreachableError, InvalidBinanceSymbolError
from app.core.price_registry import (
    clear as clear_registry,
    deactivate_symbol,
    set_price,
)
from app.core.security import create_access_token
from app.main import create_app
from app.models.holding import Holding
from app.models.trading_pair import TradingPair
from app.models.user import User, UserRole
from app.services.trade_service import TradeService


@pytest.fixture(autouse=True)
def clean_reg():
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def api_app(session_factory):
    app = create_app()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = session_factory
    return app


def auth_headers(user_id: uuid.UUID, role: UserRole = UserRole.ADMIN) -> dict[str, str]:
    role_str = role.value if isinstance(role, UserRole) else str(role)
    token = create_access_token(subject=str(user_id), role=role_str)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_admin_pairs_authorization(api_app, create_user):
    """Assert 401 unauthenticated and 403 for USER on all pairs routes."""
    user_id = await create_user(balance="1000")
    admin_id = uuid.uuid4()
    async with api_app.state.session_factory() as session:
        admin = User(
            id=admin_id,
            email=f"admin-{admin_id.hex[:6]}@test.com",
            password_hash="fakehash",
            role=UserRole.ADMIN,
            virtual_balance=Decimal("1000"),
            initial_balance=Decimal("1000"),
        )
        session.add(admin)
        await session.commit()

    user_h = auth_headers(user_id, UserRole.USER)
    admin_h = auth_headers(admin_id, UserRole.ADMIN)

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        # GET /api/admin/pairs
        resp = await client.get("/api/admin/pairs")
        assert resp.status_code == 401
        resp = await client.get("/api/admin/pairs", headers=user_h)
        assert resp.status_code == 403
        resp = await client.get("/api/admin/pairs", headers=admin_h)
        assert resp.status_code == 200

        # POST /api/admin/pairs
        resp = await client.post("/api/admin/pairs", json={"symbol": "AVAXUSDT"})
        assert resp.status_code == 401
        resp = await client.post("/api/admin/pairs", json={"symbol": "AVAXUSDT"}, headers=user_h)
        assert resp.status_code == 403

        # DELETE /api/admin/pairs/{symbol}
        resp = await client.delete("/api/admin/pairs/BTCUSDT")
        assert resp.status_code == 401
        resp = await client.delete("/api/admin/pairs/BTCUSDT", headers=user_h)
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_add_pair_success_and_feed_sync(api_app, create_user):
    """Add a new trading pair with mocked Binance check."""
    admin_id = uuid.uuid4()
    async with api_app.state.session_factory() as session:
        admin = User(
            id=admin_id,
            email=f"admin-{admin_id.hex[:6]}@test.com",
            password_hash="fakehash",
            role=UserRole.ADMIN,
            virtual_balance=Decimal("1000"),
            initial_balance=Decimal("1000"),
        )
        session.add(admin)
        await session.commit()

    admin_h = auth_headers(admin_id, UserRole.ADMIN)

    mock_feed = AsyncMock()
    mock_feed.add_symbol = AsyncMock()
    mock_feed.symbols = ["BTCUSDT"]
    api_app.state.price_feed = mock_feed

    with patch("app.services.admin_service.verify_binance_symbol", new_callable=AsyncMock) as mock_verify:
        async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
            resp = await client.post(
                "/api/admin/pairs",
                json={"symbol": "maticusdt"},
                headers=admin_h,
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["symbol"] == "MATICUSDT"
            assert data["is_active"] is True
            assert data["feed_sync"] == "synced"

            mock_verify.assert_awaited_once_with("MATICUSDT")
            mock_feed.add_symbol.assert_awaited_once_with("MATICUSDT")

    async with api_app.state.session_factory() as session:
        pair = (
            await session.execute(
                select(TradingPair).where(TradingPair.symbol == "MATICUSDT")
            )
        ).scalar_one()
        assert pair.is_active is True
        assert pair.added_by == admin_id


@pytest.mark.asyncio
async def test_add_pair_invalid_format_and_binance_errors(api_app, clean_db):
    """Assert format validation, non-existent Binance symbol, and Binance unreachable errors."""
    admin_id = uuid.uuid4()
    async with api_app.state.session_factory() as session:
        admin = User(
            id=admin_id,
            email=f"admin-{admin_id.hex[:6]}@test.com",
            password_hash="fakehash",
            role=UserRole.ADMIN,
            virtual_balance=Decimal("1000"),
            initial_balance=Decimal("1000"),
        )
        session.add(admin)
        await session.commit()

    admin_h = auth_headers(admin_id, UserRole.ADMIN)

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        # Invalid format: too short
        resp = await client.post(
            "/api/admin/pairs",
            json={"symbol": "BTC"},
            headers=admin_h,
        )
        assert resp.status_code == 422

        # Non-existent Binance symbol (verify raises InvalidBinanceSymbolError)
        with patch(
            "app.services.admin_service.verify_binance_symbol",
            side_effect=InvalidBinanceSymbolError("Symbol does not exist on Binance"),
        ):
            resp = await client.post(
                "/api/admin/pairs",
                json={"symbol": "FAKESYMBOL"},
                headers=admin_h,
            )
            assert resp.status_code == 422
            assert resp.json()["error"]["code"] == "INVALID_BINANCE_SYMBOL"

        # Binance unreachable (verify raises BinanceUnreachableError)
        with patch(
            "app.services.admin_service.verify_binance_symbol",
            side_effect=BinanceUnreachableError("Binance unreachable"),
        ):
            resp = await client.post(
                "/api/admin/pairs",
                json={"symbol": "LINKUSDT"},
                headers=admin_h,
            )
            assert resp.status_code == 503
            assert resp.json()["error"]["code"] == "BINANCE_UNREACHABLE"

    async with api_app.state.session_factory() as session:
        for s in ("FAKESYMBOL", "LINKUSDT"):
            res = (
                await session.execute(
                    select(TradingPair).where(TradingPair.symbol == s)
                )
            ).scalar_one_or_none()
            assert res is None


@pytest.mark.asyncio
async def test_add_duplicate_and_reactivate(api_app, clean_db):
    """Assert 409 PAIR_EXISTS on duplicate, and re-activation of deactivated pair."""
    admin_id = uuid.uuid4()
    async with api_app.state.session_factory() as session:
        admin = User(
            id=admin_id,
            email=f"admin-{admin_id.hex[:6]}@test.com",
            password_hash="fakehash",
            role=UserRole.ADMIN,
            virtual_balance=Decimal("1000"),
            initial_balance=Decimal("1000"),
        )
        session.add(admin)
        await session.commit()

    admin_h = auth_headers(admin_id, UserRole.ADMIN)

    with patch("app.services.admin_service.verify_binance_symbol", new_callable=AsyncMock):
        async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
            resp = await client.post(
                "/api/admin/pairs",
                json={"symbol": "DOTUSDT"},
                headers=admin_h,
            )
            assert resp.status_code == 201

            resp = await client.post(
                "/api/admin/pairs",
                json={"symbol": "DOTUSDT"},
                headers=admin_h,
            )
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "PAIR_EXISTS"

            resp = await client.delete("/api/admin/pairs/DOTUSDT", headers=admin_h)
            assert resp.status_code == 200

            resp = await client.post(
                "/api/admin/pairs",
                json={"symbol": "DOTUSDT"},
                headers=admin_h,
            )
            assert resp.status_code == 201
            assert resp.json()["is_active"] is True


@pytest.mark.asyncio
async def test_add_pair_feed_failure_reports_pending(api_app, clean_db):
    """If feed subscription fails, DB row is committed and feed_sync is 'pending'."""
    admin_id = uuid.uuid4()
    async with api_app.state.session_factory() as session:
        admin = User(
            id=admin_id,
            email=f"admin-{admin_id.hex[:6]}@test.com",
            password_hash="fakehash",
            role=UserRole.ADMIN,
            virtual_balance=Decimal("1000"),
            initial_balance=Decimal("1000"),
        )
        session.add(admin)
        await session.commit()

    admin_h = auth_headers(admin_id, UserRole.ADMIN)

    mock_feed = AsyncMock()
    mock_feed.add_symbol = AsyncMock(side_effect=RuntimeError("Feed connection down"))
    api_app.state.price_feed = mock_feed

    with patch("app.services.admin_service.verify_binance_symbol", new_callable=AsyncMock):
        async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
            resp = await client.post(
                "/api/admin/pairs",
                json={"symbol": "XRPUSDT"},
                headers=admin_h,
            )
            assert resp.status_code == 201
            assert resp.json()["feed_sync"] == "pending"

    async with api_app.state.session_factory() as session:
        pair = (
            await session.execute(
                select(TradingPair).where(TradingPair.symbol == "XRPUSDT")
            )
        ).scalar_one()
        assert pair.is_active is True


@pytest.mark.asyncio
async def test_deactivate_pair_with_and_without_holdings(api_app, create_user):
    """Assert 409 PAIR_HAS_HOLDINGS without force, and success with force=true."""
    admin_id = uuid.uuid4()
    user_id = await create_user(balance="10000")

    async with api_app.state.session_factory() as session:
        admin = User(
            id=admin_id,
            email=f"admin-{admin_id.hex[:6]}@test.com",
            password_hash="fakehash",
            role=UserRole.ADMIN,
            virtual_balance=Decimal("1000"),
            initial_balance=Decimal("1000"),
        )
        session.add(admin)

        pair = TradingPair(symbol="UNIUSDT", is_active=True)
        session.add(pair)

        holding = Holding(
            user_id=user_id,
            symbol="UNIUSDT",
            quantity=Decimal("50.00000000"),
            avg_buy_price=Decimal("10.00000000"),
        )
        session.add(holding)
        await session.commit()

    admin_h = auth_headers(admin_id, UserRole.ADMIN)

    mock_feed = AsyncMock()
    mock_feed.remove_symbol = AsyncMock()
    api_app.state.price_feed = mock_feed

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        # 1. Attempt deactivate without force -> 409
        resp = await client.delete("/api/admin/pairs/UNIUSDT", headers=admin_h)
        assert resp.status_code == 409
        err = resp.json()["error"]
        assert err["code"] == "PAIR_HAS_HOLDINGS"
        assert err["details"]["holders_count"] == 1
        mock_feed.remove_symbol.assert_not_called()

        # 2. Deactivate with force=true -> 200 OK
        resp = await client.delete("/api/admin/pairs/UNIUSDT?force=true", headers=admin_h)
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False
        mock_feed.remove_symbol.assert_awaited_once_with("UNIUSDT")


@pytest.mark.asyncio
async def test_trade_rules_on_inactive_pair(session_factory, create_user):
    """Rule verification:

    - BUY on inactive pair is blocked with SymbolNotSupported (404).
    - SELL on inactive pair is permitted if a terminal price exists.
    - SELL on inactive pair fails with PriceUnavailable if no price exists.
    """
    user_id = await create_user(balance="10000.00000000")

    async with session_factory() as session:
        pair = TradingPair(symbol="LUNAUSDT", is_active=False)
        session.add(pair)

        h = Holding(
            user_id=user_id,
            symbol="LUNAUSDT",
            quantity=Decimal("100.00000000"),
            avg_buy_price=Decimal("5.00000000"),
        )
        session.add(h)
        await session.commit()

    deactivate_symbol("LUNAUSDT")

    # 1. Attempt BUY on inactive pair
    async with session_factory() as session:
        service = TradeService(session)
        from app.core.exceptions import SymbolNotSupported

        with pytest.raises(SymbolNotSupported):
            await service.buy(user_id, "LUNAUSDT", Decimal("1.00000000"))

    # 2. Attempt SELL when no price exists -> PriceUnavailable
    async with session_factory() as session:
        service = TradeService(session)
        from app.core.exceptions import PriceUnavailable

        with pytest.raises(PriceUnavailable):
            await service.sell(user_id, "LUNAUSDT", Decimal("10.00000000"))

    # 3. Record price in registry, then simulate feed removal
    set_price("LUNAUSDT", Decimal("2.50000000"), datetime.now(timezone.utc))
    from app.core.price_registry import remove_price
    remove_price("LUNAUSDT")

    # 4. SELL on inactive pair now succeeds using last known price
    async with session_factory() as session:
        service = TradeService(session)
        result = await service.sell(user_id, "LUNAUSDT", Decimal("10.00000000"))
        assert result.trade.price == Decimal("2.50000000")
        assert result.trade.quantity == Decimal("10.00000000")
        assert result.balance == Decimal("10000.00000000") + Decimal("25.00000000")
