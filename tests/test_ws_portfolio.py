"""Integration tests for WebSocket private portfolio stream: /ws/portfolio/{user_id}.

Verifies:
- Authentication & Origin security (reject with code 1008)
- Authorization: regular user cannot connect to another user's portfolio (code 1008)
- Admin can connect to any user's portfolio
- Initial snapshot arrives on connect (type: portfolio_update)
- Ping/pong heartbeat works
- Executing a trade via REST API delivers BOTH trade_confirmation and portfolio_update
  to that user's WebSocket
- Trades executed by user A do NOT leak to user B's WebSocket
- Notifier failure (patched to raise) never breaks the trade response or committed data
- Disconnect cleans up connection manager bookkeeping
"""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
import uuid

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import get_settings
from app.core.database import get_db
from app.core.price_registry import clear as clear_registry, set_price
from app.core.security import create_access_token
from app.main import create_app
from app.services.notifier import TradeNotifier


@pytest.fixture
def test_app(monkeypatch, clean_db, session_factory):
    """FastAPI app instance configured for WebSocket testing."""
    monkeypatch.setenv("ENABLE_PRICE_FEED", "false")
    clear_registry()
    app = create_app()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = session_factory
    yield app
    clear_registry()


def create_user_helper(client: TestClient, prefix: str = "u") -> tuple[str, str]:
    """Register and login a user; returns (user_id, access_token)."""
    email = f"{prefix}_{uuid.uuid4().hex[:6]}@test.com"
    client.post("/api/auth/register", json={"email": email, "password": "Password123!"})
    login = client.post("/api/auth/login", json={"email": email, "password": "Password123!"})
    token = login.json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    return me["id"], token


# ---------------------------------------------------------------------------
# Authentication & Authorization Tests (Code 1008)
# ---------------------------------------------------------------------------


def test_ws_portfolio_rejects_missing_token(test_app):
    user_id = str(uuid.uuid4())
    with TestClient(test_app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(f"/ws/portfolio/{user_id}") as ws:
                ws.receive_text()
        assert exc_info.value.code == 1008
        assert exc_info.value.reason == "Missing authentication token"


def test_ws_portfolio_rejects_invalid_token(test_app):
    user_id = str(uuid.uuid4())
    with TestClient(test_app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(f"/ws/portfolio/{user_id}?token=invalid.token") as ws:
                ws.receive_text()
        assert exc_info.value.code == 1008
        assert exc_info.value.reason == "Invalid token"


def test_ws_portfolio_rejects_different_user(test_app):
    """User A cannot eavesdrop on User B's portfolio."""
    with TestClient(test_app) as client:
        user_a_id, token_a = create_user_helper(client, prefix="usera")
        user_b_id, token_b = create_user_helper(client, prefix="userb")

        with pytest.raises(WebSocketDisconnect) as exc_info:
            # User A attempts to connect to User B's portfolio
            with client.websocket_connect(f"/ws/portfolio/{user_b_id}?token={token_a}") as ws:
                ws.receive_text()
        assert exc_info.value.code == 1008
        assert "cannot connect to another user's portfolio" in exc_info.value.reason


def test_ws_portfolio_allows_admin_to_any_user(test_app):
    """Admin can connect to any user's portfolio stream."""
    with TestClient(test_app) as client:
        user_a_id, _ = create_user_helper(client, prefix="target")

        # Generate an admin token
        admin_id = str(uuid.uuid4())
        admin_token = create_access_token(admin_id, role="ADMIN")

        with client.websocket_connect(f"/ws/portfolio/{user_a_id}?token={admin_token}") as ws:
            snap = ws.receive_json()
            assert snap["type"] == "portfolio_update"
            assert snap["portfolio"]["cash_balance"] == "10000.00000000"


# ---------------------------------------------------------------------------
# Initial Snapshot and Ping/Pong
# ---------------------------------------------------------------------------


def test_ws_portfolio_initial_snapshot_and_ping(test_app):
    with TestClient(test_app) as client:
        user_id, token = create_user_helper(client, prefix="snap")

        with client.websocket_connect(f"/ws/portfolio/{user_id}?token={token}") as ws:
            snap = ws.receive_json()
            assert snap["type"] == "portfolio_update"
            assert snap["portfolio"]["cash_balance"] == "10000.00000000"
            assert snap["portfolio"]["total_value"] == "10000.00000000"
            assert snap["portfolio"]["holdings"] == []

            # Ping/Pong
            ws.send_json({"action": "ping"})
            pong = ws.receive_json()
            assert pong == {"type": "pong"}


# ---------------------------------------------------------------------------
# Trade Execution Notifications: Confirmation & Portfolio Update
# ---------------------------------------------------------------------------


def test_trade_execution_delivers_confirmation_and_portfolio_update(test_app):
    """Executing a trade via REST API delivers confirmation & updated portfolio to WebSocket."""
    with TestClient(test_app) as client:
        user_id, token = create_user_helper(client, prefix="trader")
        set_price("BTCUSDT", Decimal("60000.00000000"), datetime.now(timezone.utc))

        with client.websocket_connect(f"/ws/portfolio/{user_id}?token={token}") as ws:
            # 1. Drain initial snapshot
            snap = ws.receive_json()
            assert snap["type"] == "portfolio_update"

            # 2. Execute a BUY order via REST API: 0.1 BTC = 6,000 USD
            buy_resp = client.post(
                "/api/trade/buy",
                headers={"Authorization": f"Bearer {token}"},
                json={"symbol": "BTCUSDT", "quantity": "0.10000000"},
            )
            assert buy_resp.status_code == 201

            # 3. Check for trade_confirmation and portfolio_update on the WebSocket
            msg1 = ws.receive_json()
            assert msg1["type"] == "trade_confirmation"
            assert msg1["trade"]["symbol"] == "BTCUSDT"
            assert msg1["trade"]["trade_type"] == "BUY"
            assert msg1["trade"]["quantity"] == "0.10000000"
            assert msg1["balance"] == "4000.00000000"

            msg2 = ws.receive_json()
            assert msg2["type"] == "portfolio_update"
            assert msg2["portfolio"]["cash_balance"] == "4000.00000000"
            assert msg2["portfolio"]["holdings_value"] == "6000.00000000"
            assert len(msg2["portfolio"]["holdings"]) == 1
            assert msg2["portfolio"]["holdings"][0]["symbol"] == "BTCUSDT"


def test_trade_notification_does_not_leak_to_other_users(test_app):
    """User A's trade notifications are private and never delivered to User B."""
    with TestClient(test_app) as client:
        user_a_id, token_a = create_user_helper(client, prefix="priv_a")
        user_b_id, token_b = create_user_helper(client, prefix="priv_b")
        set_price("ETHUSDT", Decimal("3000.00000000"), datetime.now(timezone.utc))

        with client.websocket_connect(f"/ws/portfolio/{user_a_id}?token={token_a}") as ws_a:
            with client.websocket_connect(f"/ws/portfolio/{user_b_id}?token={token_b}") as ws_b:
                # Drain initial snapshots
                assert ws_a.receive_json()["type"] == "portfolio_update"
                assert ws_b.receive_json()["type"] == "portfolio_update"

                # User A buys 1 ETH = 3,000 USD
                client.post(
                    "/api/trade/buy",
                    headers={"Authorization": f"Bearer {token_a}"},
                    json={"symbol": "ETHUSDT", "quantity": "1.00000000"},
                )

                # User A receives confirmation & update
                assert ws_a.receive_json()["type"] == "trade_confirmation"
                assert ws_a.receive_json()["type"] == "portfolio_update"

                # User B sends a ping; the next message received MUST be pong, NOT User A's trade!
                ws_b.send_json({"action": "ping"})
                pong = ws_b.receive_json()
                assert pong == {"type": "pong"}


# ---------------------------------------------------------------------------
# Notifier Failure Resilience
# ---------------------------------------------------------------------------


def test_notifier_failure_does_not_break_trade_or_db(test_app):
    """If TradeNotifier fails or raises, the HTTP trade response succeeds and DB commits."""
    with TestClient(test_app) as client:
        user_id, token = create_user_helper(client, prefix="failproof")
        set_price("SOLUSDT", Decimal("150.00000000"), datetime.now(timezone.utc))

        # Patch notify_trade_committed to raise an unexpected error
        with patch.object(
            TradeNotifier,
            "notify_trade_committed",
            side_effect=RuntimeError("Notification network failure!"),
        ):
            resp = client.post(
                "/api/trade/buy",
                headers={"Authorization": f"Bearer {token}"},
                json={"symbol": "SOLUSDT", "quantity": "10.00000000"},
            )
            # Must still succeed with 201 Created!
            assert resp.status_code == 201
            data = resp.json()
            assert data["trade"]["symbol"] == "SOLUSDT"
            assert data["balance"] == "8500.00000000"

        # Verify DB transaction was genuinely committed
        port_resp = client.get("/api/portfolio", headers={"Authorization": f"Bearer {token}"})
        assert port_resp.status_code == 200
        assert port_resp.json()["cash_balance"] == "8500.00000000"


# ---------------------------------------------------------------------------
# Bookkeeping Cleanup on Disconnect
# ---------------------------------------------------------------------------


def test_ws_portfolio_disconnect_cleans_up_bookkeeping(test_app):
    with TestClient(test_app) as client:
        manager = test_app.state.ws_manager
        user_id, token = create_user_helper(client, prefix="clean")
        u_uuid = uuid.UUID(user_id)

        assert manager.active_count() == 0
        assert u_uuid not in manager.user_connections

        with client.websocket_connect(f"/ws/portfolio/{user_id}?token={token}") as ws:
            # Drain initial snapshot so client and server message queues are clean
            snap = ws.receive_json()
            assert snap["type"] == "portfolio_update"
            assert manager.active_count() == 1
            assert u_uuid in manager.user_connections
            assert len(manager.user_connections[u_uuid]) == 1

        # After disconnect context exits
        assert manager.active_count() == 0
        assert u_uuid not in manager.user_connections
