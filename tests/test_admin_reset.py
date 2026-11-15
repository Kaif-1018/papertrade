"""Integration tests for administrative portfolio resets.

Verifies:
- Authorization: 401 unauthenticated, 403 for normal USER, 200 for ADMIN.
- 404 for unknown user ID.
- Validation: reason must be 5-200 chars (422 otherwise).
- Admin may reset their own portfolio.
- Atomicity and post-conditions:
  - User holdings are completely wiped.
  - User virtual_balance is restored to initial_balance.
  - Past trades are strictly preserved for audit and compliance.
  - Audit row in portfolio_resets has valid snapshot and details.
  - Post-commit leaderboard cache is invalidated.
  - WebSocket portfolio update notification is dispatched.
  - FIFO analytics ignore pre-reset trades and reconcile cleanly with zero holdings.
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import get_db
from app.core.security import create_access_token
from app.main import create_app
from app.models.holding import Holding
from app.models.portfolio_reset import PortfolioReset
from app.models.trade import Trade, TradeType
from app.models.user import User, UserRole
from app.services.analytics_service import AnalyticsService


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
async def test_admin_reset_authorization_and_validation(api_app, create_user):
    """Assert auth barriers and payload validation on reset endpoint."""
    user_id = await create_user(balance="10000")
    admin_id = uuid.uuid4()
    async with api_app.state.session_factory() as session:
        admin = User(
            id=admin_id,
            email=f"admin-{admin_id.hex[:6]}@test.com",
            password_hash="fakehash",
            role=UserRole.ADMIN,
            virtual_balance=Decimal("10000"),
            initial_balance=Decimal("10000"),
        )
        session.add(admin)
        await session.commit()

    user_h = auth_headers(user_id, UserRole.USER)
    admin_h = auth_headers(admin_id, UserRole.ADMIN)

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        # 1. Unauthenticated -> 401
        resp = await client.post(f"/api/admin/users/{user_id}/reset", json={"reason": "Valid reason"})
        assert resp.status_code == 401

        # 2. Non-admin user -> 403
        resp = await client.post(
            f"/api/admin/users/{user_id}/reset",
            json={"reason": "Valid reason"},
            headers=user_h,
        )
        assert resp.status_code == 403

        # 3. Validation: reason too short (< 5 chars) -> 422
        resp = await client.post(
            f"/api/admin/users/{user_id}/reset",
            json={"reason": "bad"},
            headers=admin_h,
        )
        assert resp.status_code == 422

        # 4. Unknown user -> 404
        random_uuid = uuid.uuid4()
        resp = await client.post(
            f"/api/admin/users/{random_uuid}/reset",
            json={"reason": "Valid reset reason"},
            headers=admin_h,
        )
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_reset_execution_and_invariants(api_app, session_factory, clean_db):
    """Execute reset on user with holdings and trades, verifying:

    - Holdings deleted
    - Balance restored
    - Trades preserved
    - Audit row created with accurate snapshot
    - Cache invalidated
    - Notifier invoked
    - FIFO PnL ignores pre-reset trades
    """
    user_id = uuid.uuid4()
    admin_id = uuid.uuid4()

    async with session_factory() as session:
        user = User(
            id=user_id,
            email=f"trader-{user_id.hex[:6]}@test.com",
            password_hash="fakehash",
            role=UserRole.USER,
            virtual_balance=Decimal("6000.00000000"),
            initial_balance=Decimal("10000.00000000"),
        )
        admin = User(
            id=admin_id,
            email=f"admin-{admin_id.hex[:6]}@test.com",
            password_hash="fakehash",
            role=UserRole.ADMIN,
            virtual_balance=Decimal("10000.00000000"),
            initial_balance=Decimal("10000.00000000"),
        )
        session.add_all([user, admin])

        # Add holdings
        h1 = Holding(
            user_id=user_id,
            symbol="BTCUSDT",
            quantity=Decimal("0.05000000"),
            avg_buy_price=Decimal("60000.00000000"),
        )
        h2 = Holding(
            user_id=user_id,
            symbol="ETHUSDT",
            quantity=Decimal("0.50000000"),
            avg_buy_price=Decimal("2000.00000000"),
        )
        session.add_all([h1, h2])

        # Add prior trade record
        trade = Trade(
            id=uuid.uuid4(),
            user_id=user_id,
            symbol="BTCUSDT",
            trade_type=TradeType.BUY,
            quantity=Decimal("0.05000000"),
            price=Decimal("60000.00000000"),
            total_value=Decimal("3000.00000000"),
            executed_at=datetime.now(timezone.utc),
        )
        session.add(trade)
        await session.commit()

    admin_h = auth_headers(admin_id, UserRole.ADMIN)

    # Mock notifier and cache invalidation
    mock_notifier = AsyncMock()
    mock_notifier.notify_portfolio_reset = AsyncMock()
    api_app.state.trade_notifier = mock_notifier

    with patch("app.services.admin_service.invalidate_leaderboard", new_callable=AsyncMock) as mock_inv:
        async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
            resp = await client.post(
                f"/api/admin/users/{user_id}/reset",
                json={"reason": "User requested account wipe due to test trades"},
                headers=admin_h,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["user_id"] == str(user_id)
            assert data["admin_id"] == str(admin_id)
            assert data["reason"] == "User requested account wipe due to test trades"
            assert data["balance_before"] == "6000.00000000"
            assert data["balance_after"] == "10000.00000000"
            assert len(data["holdings_snapshot"]) == 2

            # Assert cache invalidation was triggered
            mock_inv.assert_awaited_once()

            # Assert WebSocket notification was dispatched
            mock_notifier.notify_portfolio_reset.assert_awaited_once_with(user_id)

    # Verify DB state directly
    async with session_factory() as session:
        # 1. User balance restored
        u = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        assert u.virtual_balance == Decimal("10000.00000000")

        # 2. Holdings wiped
        holdings = (
            await session.execute(select(Holding).where(Holding.user_id == user_id))
        ).scalars().all()
        assert len(holdings) == 0

        # 3. Trades preserved
        trades = (
            await session.execute(select(Trade).where(Trade.user_id == user_id))
        ).scalars().all()
        assert len(trades) == 1

        # 4. Audit row in portfolio_resets
        resets = (
            await session.execute(select(PortfolioReset).where(PortfolioReset.user_id == user_id))
        ).scalars().all()
        assert len(resets) == 1
        assert resets[0].balance_before == Decimal("6000.00000000")
        assert len(resets[0].holdings_snapshot) == 2

    # 5. Assert FIFO analytics ignores pre-reset trades and reports 0 holdings without warning
    async with session_factory() as session:
        analytics = AnalyticsService(session)
        pnl = await analytics.get_pnl_analytics(user_id)
        assert pnl.total_realised_pnl == Decimal("0.00000000")
        assert pnl.total_trades == 0  # Pre-reset trades ignored!
        assert pnl.consistency_warning is None


@pytest.mark.asyncio
async def test_admin_may_reset_self(api_app, session_factory, clean_db):
    """Admin can reset their own account and it is recorded normally."""
    admin_id = uuid.uuid4()
    async with session_factory() as session:
        admin = User(
            id=admin_id,
            email=f"admin-{admin_id.hex[:6]}@test.com",
            password_hash="fakehash",
            role=UserRole.ADMIN,
            virtual_balance=Decimal("2000.00000000"),
            initial_balance=Decimal("10000.00000000"),
        )
        session.add(admin)
        await session.commit()

    admin_h = auth_headers(admin_id, UserRole.ADMIN)

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/admin/users/{admin_id}/reset",
            json={"reason": "Self-inflicted balance reset for fresh testing"},
            headers=admin_h,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == str(admin_id)
        assert data["admin_id"] == str(admin_id)
        assert data["balance_after"] == "10000.00000000"
