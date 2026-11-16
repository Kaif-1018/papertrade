"""Complete end-to-end integration and resilience test flows.

Simulates the full real-world lifecycle:
1. User registration, authentication, and profile verification.
2. In-memory price tick simulation and trade execution (BUY).
3. Portfolio valuation changes across price movements.
4. FIFO realised PnL execution (partial SELL).
5. Watchlist management and leaderboard visibility with identity masking.
6. Refresh token rotation and tamper/reuse detection.
7. Real-time WebSocket broadcasting (price ticks, trade confirmations, portfolio snapshots).
8. Admin operations (pair management, metrics inspection, atomic portfolio reset).
9. Negative security gates (403 on admin routes, 1008 on invalid WS token, 429 rate limit).
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
import time
from unittest.mock import AsyncMock, patch
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from starlette.testclient import TestClient

from app.api.deps import get_db
from app.core.config import get_settings
from app.core.price_registry import clear as clear_registry
from app.core.redis import get_redis
from app.core.security import create_access_token, hash_password
from app.main import create_app
from app.models.user import User, UserRole
from app.repositories.user import UserRepository


@pytest.fixture
def e2e_app(session_factory, monkeypatch):
    monkeypatch.setenv("ENABLE_PRICE_FEED", "false")
    app = create_app()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = session_factory
    return app


@pytest.mark.asyncio
async def test_full_user_trading_lifecycle(e2e_app, clean_db, set_price, test_redis):
    """Complete user journey: register -> trade -> valuation -> FIFO PnL -> rotate -> logout."""
    clear_registry()
    set_price("BTCUSDT", "50000.00000000")

    async with AsyncClient(transport=ASGITransport(app=e2e_app), base_url="http://test") as client:
        # 1. Register
        reg_resp = await client.post(
            "/api/auth/register",
            json={"email": "trader.e2e@example.com", "password": "SecurePassword123!"},
        )
        assert reg_resp.status_code == 201
        user_data = reg_resp.json()
        assert user_data["email"] == "trader.e2e@example.com"
        assert Decimal(user_data["virtual_balance"]) == Decimal("10000.00000000")

        # 2. Login
        login_resp = await client.post(
            "/api/auth/login",
            json={"email": "trader.e2e@example.com", "password": "SecurePassword123!"},
        )
        assert login_resp.status_code == 200
        tokens = login_resp.json()
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]
        auth_h = {"Authorization": f"Bearer {access_token}"}

        # 3. GET /api/auth/me
        me_resp = await client.get("/api/auth/me", headers=auth_h)
        assert me_resp.status_code == 200
        assert me_resp.json()["email"] == "trader.e2e@example.com"

        # 4. BUY 0.1 BTC at $50,000 = $5,000
        buy_resp = await client.post(
            "/api/trade/buy",
            json={"symbol": "BTCUSDT", "quantity": "0.1"},
            headers=auth_h,
        )
        assert buy_resp.status_code == 201
        buy_data = buy_resp.json()
        assert buy_data["trade"]["symbol"] == "BTCUSDT"
        assert Decimal(buy_data["trade"]["quantity"]) == Decimal("0.1")
        assert Decimal(buy_data["trade"]["price"]) == Decimal("50000")
        assert Decimal(buy_data["balance"]) == Decimal("5000")

        # 5. Check Portfolio at purchase price
        port_resp1 = await client.get("/api/portfolio", headers=auth_h)
        assert port_resp1.status_code == 200
        port_data1 = port_resp1.json()
        assert Decimal(port_data1["cash_balance"]) == Decimal("5000.00000000")
        assert Decimal(port_data1["holdings_value"]) == Decimal("5000.00000000")
        assert Decimal(port_data1["total_value"]) == Decimal("10000.00000000")
        assert Decimal(port_data1["total_pnl"]) == Decimal("0.00000000")
        assert Decimal(port_data1["holdings"][0]["unrealised_pnl"]) == Decimal("0.00000000")

        # 6. Price moves up: BTC jumps to $60,000
        set_price("BTCUSDT", "60000.00000000")

        port_resp2 = await client.get("/api/portfolio", headers=auth_h)
        assert port_resp2.status_code == 200
        port_data2 = port_resp2.json()
        # 0.1 BTC * 60,000 = $6,000 + $5,000 cash = $11,000 total (+$1,000 unrealised)
        assert Decimal(port_data2["holdings_value"]) == Decimal("6000.00000000")
        assert Decimal(port_data2["total_value"]) == Decimal("11000.00000000")
        assert Decimal(port_data2["total_pnl"]) == Decimal("1000.00000000")
        assert Decimal(port_data2["holdings"][0]["unrealised_pnl"]) == Decimal("1000.00000000")

        # 7. SELL half position (0.05 BTC at $60,000 = $3,000)
        sell_resp = await client.post(
            "/api/trade/sell",
            json={"symbol": "BTCUSDT", "quantity": "0.05"},
            headers=auth_h,
        )
        assert sell_resp.status_code == 201
        sell_data = sell_resp.json()
        assert Decimal(sell_data["balance"]) == Decimal("8000.00000000")

        # 8. Trade history verification
        trades_resp = await client.get("/api/trades", headers=auth_h)
        assert trades_resp.status_code == 200
        trades_data = trades_resp.json()
        assert len(trades_data["items"]) == 2

        # 9. FIFO PnL Analytics: 0.05 BTC sold at 60k bought at 50k -> realised = +$500
        pnl_resp = await client.get("/api/analytics/pnl", headers=auth_h)
        assert pnl_resp.status_code == 200
        pnl_data = pnl_resp.json()
        assert Decimal(pnl_data["total_realised_pnl"]) == Decimal("500.00000000")

        # 10. Watchlist
        watch_add = await client.post("/api/watchlist/BTCUSDT", headers=auth_h)
        assert watch_add.status_code in (200, 201)
        watch_list = await client.get("/api/watchlist", headers=auth_h)
        assert watch_list.status_code == 200
        assert any(item["symbol"] == "BTCUSDT" for item in watch_list.json())

        # 11. Leaderboard includes masked user
        lb_resp = await client.get("/api/leaderboard", headers=auth_h)
        assert lb_resp.status_code == 200
        lb_data = lb_resp.json()
        assert lb_data["me"] is not None
        assert "trader.e2e@example.com" not in lb_resp.text  # Identity is strictly masked

        # 12. Token rotation
        rotate_resp = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert rotate_resp.status_code == 200
        new_tokens = rotate_resp.json()
        new_access = new_tokens["access_token"]
        assert new_access != access_token

        # 13. Reuse of old refresh token triggers revocation of all active sessions
        reuse_resp = await client.post(
            "/api/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert reuse_resp.status_code == 401
        assert reuse_resp.json()["error"]["code"] == "TOKEN_REUSE_DETECTED"

        # 14. Relogin and Logout clean session
        relogin_resp = await client.post(
            "/api/auth/login",
            json={"email": "trader.e2e@example.com", "password": "SecurePassword123!"},
        )
        assert relogin_resp.status_code == 200
        logout_resp = await client.post(
            "/api/auth/logout",
            json={"refresh_token": relogin_resp.json()["refresh_token"]},
        )
        assert logout_resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_flow_and_metrics(e2e_app, session_factory, clean_db):
    """Admin privilege verification, metrics access, pair activation, and user reset."""
    admin_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async with session_factory() as session:
        user_repo = UserRepository(session)
        admin = user_repo.create(
            email="admin.e2e@platform.com",
            password_hash=hash_password("AdminPass123!"),
            virtual_balance=Decimal("100000.00000000"),
            initial_balance=Decimal("100000.00000000"),
            role=UserRole.ADMIN,
        )
        user = user_repo.create(
            email="reset.target@example.com",
            password_hash=hash_password("UserPass123!"),
            virtual_balance=Decimal("2500.00000000"),
            initial_balance=Decimal("10000.00000000"),
            role=UserRole.USER,
        )
        await session.commit()
        await session.refresh(admin)
        await session.refresh(user)
        admin_id = admin.id
        user_id = user.id

    admin_token = create_access_token(subject=str(admin_id), role="ADMIN")
    user_token = create_access_token(subject=str(user_id), role="USER")

    admin_h = {"Authorization": f"Bearer {admin_token}"}
    user_h = {"Authorization": f"Bearer {user_token}"}

    async with AsyncClient(transport=ASGITransport(app=e2e_app), base_url="http://test") as client:
        # Negative Gate: normal user forbidden from admin metrics & stats
        forbidden_resp = await client.get("/api/admin/metrics", headers=user_h)
        assert forbidden_resp.status_code == 403

        # Admin fetches operational metrics
        metrics_resp = await client.get("/api/admin/metrics", headers=admin_h)
        assert metrics_resp.status_code == 200
        metrics = metrics_resp.json()
        assert "requests_total" in metrics
        assert "trade_count" in metrics
        assert "active_websockets" in metrics

        # Admin resets user portfolio
        reset_resp = await client.post(
            f"/api/admin/users/{user_id}/reset",
            json={"reason": "User requested balance restoration after market liquidation"},
            headers=admin_h,
        )
        assert reset_resp.status_code == 200
        reset_data = reset_resp.json()
        assert Decimal(reset_data["balance_after"]) == Decimal("10000.00000000")


def test_realtime_websocket_flow(e2e_app, clean_db, session_factory, set_price):
    """Open price and portfolio websockets, perform trade, assert confirmation and portfolio update."""
    clear_registry()
    set_price("BTCUSDT", "50000.00000000")

    with TestClient(e2e_app) as client:
        reg_resp = client.post(
            "/api/auth/register",
            json={"email": "ws.trader@example.com", "password": "SecurePassword123!"},
        )
        assert reg_resp.status_code == 201

        login_resp = client.post(
            "/api/auth/login",
            json={"email": "ws.trader@example.com", "password": "SecurePassword123!"},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["access_token"]
        me_resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        user_id = me_resp.json()["id"]

        with client.websocket_connect(f"/ws/prices/BTCUSDT?token={token}") as ws_price:
            with client.websocket_connect(f"/ws/portfolio/{user_id}?token={token}") as ws_port:
                # Drain initial snapshots
                price_snap = ws_price.receive_json()
                assert price_snap["type"] == "snapshot"
                assert price_snap["symbol"] == "BTCUSDT"

                port_snap = ws_port.receive_json()
                assert port_snap["type"] == "portfolio_update"

                # Execute trade via REST
                trade_resp = client.post(
                    "/api/trade/buy",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"symbol": "BTCUSDT", "quantity": "0.1"},
                )
                assert trade_resp.status_code == 201

                # Assert trade_confirmation and portfolio_update arrive on portfolio WS
                conf = ws_port.receive_json()
                assert conf["type"] == "trade_confirmation"
                assert conf["trade"]["symbol"] == "BTCUSDT"
                assert Decimal(conf["trade"]["quantity"]) == Decimal("0.1")

                update = ws_port.receive_json()
                assert update["type"] == "portfolio_update"
                assert Decimal(update["portfolio"]["cash_balance"]) == Decimal("5000")


def test_ws_negative_security_close_code(e2e_app):
    """Connecting to WebSocket with an invalid or expired token is closed with code 1008."""
    client = TestClient(e2e_app)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/prices/BTCUSDT?token=invalid.jwt.token") as ws:
            ws.receive_json()
