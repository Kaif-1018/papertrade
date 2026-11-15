"""Integration tests for platform statistics aggregation endpoint.

Verifies:
- Authorization: 401 unauthenticated, 403 for USER, 200 for ADMIN.
- Exact SQL aggregate values:
  - total_users
  - total_trades
  - volume_all_time
  - volume_24h (correctly excludes trades older than 24h)
  - trades_24h (correctly excludes trades older than 24h)
  - active_traders_24h (distinct count within 24h window)
  - top_symbols_by_volume (top symbols ordered by volume DESC)
  - active_ws_connections and topic breakdown
  - price feed health status
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db
from app.core.security import create_access_token
from app.main import create_app
from app.models.trade import Trade, TradeType
from app.models.user import User, UserRole


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
async def test_admin_stats_authorization(api_app, create_user):
    """Assert 401 unauthenticated and 403 for normal USER."""
    user_id = await create_user(balance="1000")
    user_h = auth_headers(user_id, UserRole.USER)

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        # Unauthenticated -> 401
        resp = await client.get("/api/admin/stats")
        assert resp.status_code == 401

        # User -> 403
        resp = await client.get("/api/admin/stats", headers=user_h)
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_stats_exact_aggregates(api_app, session_factory, clean_db):
    """Assert exact metrics and proper exclusion of historical data outside 24h window."""
    admin_id = uuid.uuid4()
    u1_id = uuid.uuid4()
    u2_id = uuid.uuid4()
    u3_id = uuid.uuid4()

    now = datetime.now(timezone.utc)
    old_time = now - timedelta(hours=48)  # Older than 24h
    recent_time1 = now - timedelta(hours=2)
    recent_time2 = now - timedelta(minutes=15)

    async with session_factory() as session:
        admin = User(
            id=admin_id,
            email=f"admin-{admin_id.hex[:6]}@stats.com",
            password_hash="fakehash",
            role=UserRole.ADMIN,
            virtual_balance=Decimal("10000"),
            initial_balance=Decimal("10000"),
        )
        u1 = User(
            id=u1_id,
            email=f"u1-{u1_id.hex[:6]}@stats.com",
            password_hash="fakehash",
            role=UserRole.USER,
            virtual_balance=Decimal("10000"),
            initial_balance=Decimal("10000"),
        )
        u2 = User(
            id=u2_id,
            email=f"u2-{u2_id.hex[:6]}@stats.com",
            password_hash="fakehash",
            role=UserRole.USER,
            virtual_balance=Decimal("10000"),
            initial_balance=Decimal("10000"),
        )
        u3 = User(
            id=u3_id,
            email=f"u3-{u3_id.hex[:6]}@stats.com",
            password_hash="fakehash",
            role=UserRole.USER,
            virtual_balance=Decimal("10000"),
            initial_balance=Decimal("10000"),
        )
        session.add_all([admin, u1, u2, u3])

        # Trade 1: U1 recent trade (BTCUSDT: 1000.00)
        t1 = Trade(
            id=uuid.uuid4(),
            user_id=u1_id,
            symbol="BTCUSDT",
            trade_type=TradeType.BUY,
            quantity=Decimal("0.02000000"),
            price=Decimal("50000.00000000"),
            total_value=Decimal("1000.00000000"),
            executed_at=recent_time1,
        )
        # Trade 2: U1 old trade (BTCUSDT: 500.00) - older than 24h
        t2 = Trade(
            id=uuid.uuid4(),
            user_id=u1_id,
            symbol="BTCUSDT",
            trade_type=TradeType.BUY,
            quantity=Decimal("0.01000000"),
            price=Decimal("50000.00000000"),
            total_value=Decimal("500.00000000"),
            executed_at=old_time,
        )
        # Trade 3: U2 recent trade (ETHUSDT: 2500.00)
        t3 = Trade(
            id=uuid.uuid4(),
            user_id=u2_id,
            symbol="ETHUSDT",
            trade_type=TradeType.BUY,
            quantity=Decimal("1.00000000"),
            price=Decimal("2500.00000000"),
            total_value=Decimal("2500.00000000"),
            executed_at=recent_time2,
        )
        session.add_all([t1, t2, t3])
        await session.commit()

    # Mock ws_manager and price_feed
    mock_ws = MagicMock()
    mock_ws.active_count.return_value = 7
    mock_ws.topic_counts.return_value = {"BTCUSDT": 4, "ETHUSDT": 3}
    api_app.state.ws_manager = mock_ws

    mock_feed = MagicMock()

    class StatusObj:
        connected = True
        reconnect_count = 0

    mock_feed.get_status.return_value = StatusObj()
    api_app.state.price_feed = mock_feed

    admin_h = auth_headers(admin_id, UserRole.ADMIN)

    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        resp = await client.get("/api/admin/stats", headers=admin_h)
        assert resp.status_code == 200
        data = resp.json()

        assert data["total_users"] == 4
        assert data["total_trades"] == 3
        # Volume all time = 1000 + 500 + 2500 = 4000.00000000
        assert data["volume_all_time"] == "4000.00000000"
        # Volume 24h = 1000 + 2500 = 3500.00000000 (excludes t2)
        assert data["volume_24h"] == "3500.00000000"
        # Trades 24h = 2 (t1 and t3)
        assert data["trades_24h"] == 2
        # Active traders 24h = 2 (u1 and u2)
        assert data["active_traders_24h"] == 2

        # Top symbols
        top_symbols = data["top_symbols_by_volume"]
        assert len(top_symbols) >= 2
        assert top_symbols[0]["symbol"] == "ETHUSDT"
        assert top_symbols[0]["volume"] == "2500.00000000"
        assert top_symbols[0]["trade_count"] == 1
        assert top_symbols[1]["symbol"] == "BTCUSDT"
        assert top_symbols[1]["volume"] == "1500.00000000"
        assert top_symbols[1]["trade_count"] == 2

        # WS stats
        assert data["active_ws_connections"] == 7
        assert data["ws_topics_breakdown"] == {"BTCUSDT": 4, "ETHUSDT": 3}
        assert data["feed_health"]["connected"] is True
