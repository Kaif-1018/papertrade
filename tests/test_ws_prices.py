"""Integration tests for WebSocket price broadcasting endpoint: /ws/prices/{symbol}.

Verifies:
- Authentication & Origin security (reject with code 1008)
- Path symbol validation (reject with code 1008)
- Initial snapshot delivery on connect
- Ping/Pong action loop
- Dynamic subscribe and unsubscribe with new snapshot delivery
- Error handling for invalid JSON, unknown actions, and unsupported symbols
- Per-connection subscription cap enforcement
- Clean connection lifecycle and manager bookkeeping on disconnect
- Real-time Redis pubsub tick broadcast delivery to WebSocket clients
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import time
import uuid

import jwt
import pytest
import redis
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import get_settings
from app.core.price_registry import clear as clear_registry, set_price
from app.core.security import create_access_token, create_refresh_token
from app.main import create_app


@pytest.fixture
def auth_user_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def valid_token(auth_user_id: str) -> str:
    return create_access_token(auth_user_id, role="USER")


@pytest.fixture
def test_app(monkeypatch):
    """FastAPI app instance configured for WebSocket testing."""
    monkeypatch.setenv("ENABLE_PRICE_FEED", "false")
    clear_registry()
    app = create_app()
    yield app
    clear_registry()


def receive_matching(ws, expected_type: str, timeout: float = 5.0) -> dict:
    """Receive messages until one matches expected_type, ignoring background live ticks."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = ws.receive_json()
        if msg.get("type") == expected_type:
            return msg
    raise TimeoutError(f"Did not receive message of type {expected_type} within {timeout}s")


# ---------------------------------------------------------------------------
# Authentication & Validation Rejection Tests (Close Code 1008)
# ---------------------------------------------------------------------------


def test_ws_rejects_missing_token(test_app):
    with TestClient(test_app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/prices/BTCUSDT") as ws:
                ws.receive_text()
        assert exc_info.value.code == 1008
        assert exc_info.value.reason == "Missing authentication token"


def test_ws_rejects_invalid_token(test_app):
    with TestClient(test_app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws/prices/BTCUSDT?token=malformed.invalid.token") as ws:
                ws.receive_text()
        assert exc_info.value.code == 1008
        assert exc_info.value.reason == "Invalid token"


def test_ws_rejects_expired_token(test_app, auth_user_id):
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expired_payload = {
        "sub": auth_user_id,
        "role": "USER",
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": now - timedelta(hours=2),
        "exp": now - timedelta(hours=1),
    }
    expired_token = jwt.encode(
        expired_payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM
    )

    with TestClient(test_app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(f"/ws/prices/BTCUSDT?token={expired_token}") as ws:
                ws.receive_text()
        assert exc_info.value.code == 1008
        assert exc_info.value.reason == "Token has expired"


def test_ws_rejects_refresh_token(test_app, auth_user_id):
    refresh_token, _ = create_refresh_token(auth_user_id, role="USER")

    with TestClient(test_app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(f"/ws/prices/BTCUSDT?token={refresh_token}") as ws:
                ws.receive_text()
        assert exc_info.value.code == 1008
        assert exc_info.value.reason == "Token type must be access"


def test_ws_rejects_unsupported_path_symbol(test_app, valid_token):
    with TestClient(test_app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(f"/ws/prices/INVALIDCOIN?token={valid_token}") as ws:
                ws.receive_text()
        assert exc_info.value.code == 1008
        assert "is not supported" in exc_info.value.reason


def test_ws_rejects_disallowed_origin(monkeypatch, valid_token):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://allowed.example.com")
    monkeypatch.setenv("ENABLE_PRICE_FEED", "false")
    app = create_app()

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/ws/prices/BTCUSDT?token={valid_token}",
                headers={"Origin": "https://malicious.example.com"},
            ) as ws:
                ws.receive_text()
        assert exc_info.value.code == 1008
        assert exc_info.value.reason == "Origin not allowed"


# ---------------------------------------------------------------------------
# Successful Connection, Snapshots, and Client Actions
# ---------------------------------------------------------------------------


def test_ws_connect_delivers_initial_snapshot(test_app, valid_token):
    now = datetime.now(timezone.utc)
    set_price("BTCUSDT", Decimal("67450.50"), now)

    with TestClient(test_app) as client:
        with client.websocket_connect(f"/ws/prices/BTCUSDT?token={valid_token}") as ws:
            snap = receive_matching(ws, "snapshot")
            assert snap["type"] == "snapshot"
            assert snap["symbol"] == "BTCUSDT"
            assert snap["price"] == "67450.50"
            assert "event_time" in snap
            assert isinstance(snap["age_ms"], int)
            assert snap["age_ms"] >= 0


def test_ws_ping_pong(test_app, valid_token):
    with TestClient(test_app) as client:
        with client.websocket_connect(f"/ws/prices/BTCUSDT?token={valid_token}") as ws:
            ws.send_json({"action": "ping"})
            pong = receive_matching(ws, "pong")
            assert pong == {"type": "pong"}


def test_ws_subscribe_and_unsubscribe(test_app, valid_token):
    now = datetime.now(timezone.utc)
    set_price("ETHUSDT", Decimal("3520.10"), now)

    with TestClient(test_app) as client:
        with client.websocket_connect(f"/ws/prices/BTCUSDT?token={valid_token}") as ws:
            # Dynamic subscribe to ETHUSDT
            ws.send_json({"action": "subscribe", "symbol": "ETHUSDT"})
            ack = receive_matching(ws, "subscribed")
            assert ack == {"type": "subscribed", "symbol": "ETHUSDT"}

            # Snapshot delivered for newly subscribed symbol
            snap = receive_matching(ws, "snapshot")
            assert snap["type"] == "snapshot"
            assert snap["symbol"] == "ETHUSDT"
            assert snap["price"] == "3520.10"

            # Dynamic unsubscribe
            ws.send_json({"action": "unsubscribe", "symbol": "ETHUSDT"})
            unsub_ack = receive_matching(ws, "unsubscribed")
            assert unsub_ack == {"type": "unsubscribed", "symbol": "ETHUSDT"}


def test_ws_subscribe_unsupported_symbol(test_app, valid_token):
    with TestClient(test_app) as client:
        with client.websocket_connect(f"/ws/prices/BTCUSDT?token={valid_token}") as ws:
            ws.send_json({"action": "subscribe", "symbol": "FAKECOIN"})
            err = receive_matching(ws, "error")
            assert err["type"] == "error"
            assert err["code"] == "SYMBOL_NOT_SUPPORTED"

            # Connection must remain alive
            ws.send_json({"action": "ping"})
            pong = receive_matching(ws, "pong")
            assert pong == {"type": "pong"}


def test_ws_invalid_json_and_unknown_action(test_app, valid_token):
    with TestClient(test_app) as client:
        with client.websocket_connect(f"/ws/prices/BTCUSDT?token={valid_token}") as ws:
            # Send malformed string
            ws.send_text("this is not json { [")
            err1 = receive_matching(ws, "error")
            assert err1["type"] == "error"
            assert err1["code"] == "INVALID_JSON"

            # Send non-dict JSON
            ws.send_text("[1, 2, 3]")
            err2 = receive_matching(ws, "error")
            assert err2["type"] == "error"
            assert err2["code"] == "INVALID_FORMAT"

            # Send unknown action
            ws.send_json({"action": "dance_party"})
            err3 = receive_matching(ws, "error")
            assert err3["type"] == "error"
            assert err3["code"] == "UNKNOWN_ACTION"

            # Verify connection is still open and functional
            ws.send_json({"action": "ping"})
            pong = receive_matching(ws, "pong")
            assert pong == {"type": "pong"}


def test_ws_subscription_limit_enforced(test_app, valid_token):
    with TestClient(test_app) as client:
        # Override connection manager's cap to 3 for this test
        manager = test_app.state.ws_manager
        manager.max_subscriptions_per_conn = 3

        with client.websocket_connect(f"/ws/prices/BTCUSDT?token={valid_token}") as ws:
            # 1 topic already subscribed via path (BTCUSDT)
            # Subscribe to 2nd: ETHUSDT
            ws.send_json({"action": "subscribe", "symbol": "ETHUSDT"})
            assert receive_matching(ws, "subscribed")["type"] == "subscribed"

            # Subscribe to 3rd: SOLUSDT
            ws.send_json({"action": "subscribe", "symbol": "SOLUSDT"})
            assert receive_matching(ws, "subscribed")["type"] == "subscribed"

            # Attempt 4th subscription: BNBUSDT -> must hit SUBSCRIPTION_LIMIT
            ws.send_json({"action": "subscribe", "symbol": "BNBUSDT"})
            err = receive_matching(ws, "error")
            assert err["type"] == "error"
            assert err["code"] == "SUBSCRIPTION_LIMIT"
            assert "Maximum topic subscriptions" in err["message"]


def test_ws_disconnect_cleans_up_bookkeeping(test_app, valid_token):
    with TestClient(test_app) as client:
        manager = test_app.state.ws_manager
        assert manager.active_count() == 0

        with client.websocket_connect(f"/ws/prices/BTCUSDT?token={valid_token}"):
            assert manager.active_count() == 1
            assert "BTCUSDT" in manager.topics
            assert manager.topic_counts().get("BTCUSDT") == 1

        # After exiting context, socket is closed and bookkeeping is pruned
        assert manager.active_count() == 0
        assert "BTCUSDT" not in manager.topics


# ---------------------------------------------------------------------------
# Redis Pub/Sub Live Price Broadcast Integration Test
# ---------------------------------------------------------------------------


def test_ws_redis_publish_broadcasts_live_tick(test_app, valid_token):
    settings = get_settings()
    r_sync = redis.from_url(settings.REDIS_URL)

    with TestClient(test_app) as client:
        with client.websocket_connect(f"/ws/prices/BTCUSDT?token={valid_token}") as ws:
            time.sleep(0.15)  # Allow pubsub subscription to register

            now_iso = datetime.now(timezone.utc).isoformat()
            payload = {
                "type": "price",
                "symbol": "BTCUSDT",
                "price": "69123.45000000",
                "ts": now_iso,
            }

            # Publish to Redis prices:BTCUSDT
            r_sync.publish("prices:BTCUSDT", json.dumps(payload))

            # WebSocket client receives broadcast
            tick = ws.receive_json()
            assert tick["type"] == "price"
            assert tick["symbol"] == "BTCUSDT"
            assert tick["price"] == "69123.45000000"
            assert tick["ts"] == now_iso

            # Publish to a symbol the client is NOT subscribed to (SOLUSDT)
            sol_payload = {
                "type": "price",
                "symbol": "SOLUSDT",
                "price": "185.00000000",
                "ts": now_iso,
            }
            r_sync.publish("prices:SOLUSDT", json.dumps(sol_payload))

            # Ping to verify the queue only has ping response (SOLUSDT wasn't received)
            ws.send_json({"action": "ping"})
            resp = ws.receive_json()
            assert resp == {"type": "pong"}
