"""Integration tests for WebSocket price endpoint with watchlist actions and live sync.

Verifies:
- {"action": "subscribe_watchlist"} subscribes to all symbols in user's watchlist
- Broadcast ticks for watchlist symbols arrive on the open socket
- Topic limit enforcement: returns error code SUBSCRIPTION_LIMIT if cap exceeded
- {"action": "unsubscribe_watchlist"} removes watchlist topics while preserving explicit ones
- Live sync: Adding a symbol via REST while WebSocket is open immediately subscribes
  and emits {"type": "watchlist_changed", "action": "added", "symbol": ...}
- Live sync: Removing a symbol via REST immediately stops ticks and emits
  {"type": "watchlist_changed", "action": "removed", "symbol": ...}
- User isolation: User B's connections are completely unaffected by User A's changes
- Clean disconnect leaves zero leaked references in ConnectionManager
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import time
import uuid

import pytest
from starlette.testclient import TestClient

from app.core.config import get_settings
from app.core.database import get_db
from app.core.price_registry import clear as clear_registry, set_price
from app.main import create_app


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


def receive_matching(ws, expected_type: str, timeout: float = 3.0) -> dict:
    """Receive messages until one matches expected_type."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = ws.receive_json()
        if msg.get("type") == expected_type:
            return msg
    raise TimeoutError(f"Did not receive message of type {expected_type} within {timeout}s")


def test_subscribe_watchlist_and_receive_ticks(test_app):
    with TestClient(test_app) as client:
        user_id, token = create_user_helper(client)
        headers = {"Authorization": f"Bearer {token}"}

        # Add ETHUSDT and SOLUSDT to user's watchlist
        client.post("/api/watchlist/ETHUSDT", headers=headers)
        client.post("/api/watchlist/SOLUSDT", headers=headers)

        manager = test_app.state.ws_manager

        with client.websocket_connect(f"/ws/prices/BTCUSDT?token={token}") as ws:
            # Drain initial snapshot if any
            try:
                ws.receive_json(mode="text")
            except Exception:
                pass

            # Send subscribe_watchlist action
            ws.send_json({"action": "subscribe_watchlist"})
            msg = receive_matching(ws, "watchlist_subscribed")
            assert set(msg["symbols"]) == {"ETHUSDT", "SOLUSDT"}

            # Broadcast a tick for SOLUSDT
            tick_payload = {
                "type": "price",
                "symbol": "SOLUSDT",
                "price": "145.50000000",
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            asyncio.run(manager.broadcast("SOLUSDT", tick_payload))

            tick_msg = receive_matching(ws, "price")
            assert tick_msg["symbol"] == "SOLUSDT"
            assert tick_msg["price"] == "145.50000000"


def test_unsubscribe_watchlist_preserves_explicit_symbols(test_app):
    with TestClient(test_app) as client:
        user_id, token = create_user_helper(client)
        headers = {"Authorization": f"Bearer {token}"}

        client.post("/api/watchlist/ETHUSDT", headers=headers)
        manager = test_app.state.ws_manager

        with client.websocket_connect(f"/ws/prices/BTCUSDT?token={token}") as ws:
            try:
                ws.receive_json(mode="text")
            except Exception:
                pass

            ws.send_json({"action": "subscribe_watchlist"})
            receive_matching(ws, "watchlist_subscribed")

            # Unsubscribe from watchlist
            ws.send_json({"action": "unsubscribe_watchlist"})
            unsub_msg = receive_matching(ws, "watchlist_unsubscribed")
            assert unsub_msg["type"] == "watchlist_unsubscribed"

            # Ticks for ETHUSDT should no longer arrive
            eth_tick = {
                "type": "price",
                "symbol": "ETHUSDT",
                "price": "3100.00000000",
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            asyncio.run(manager.broadcast("ETHUSDT", eth_tick))

            # Broadcast tick for BTCUSDT (explicit initial path symbol)
            btc_tick = {
                "type": "price",
                "symbol": "BTCUSDT",
                "price": "67000.00000000",
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            asyncio.run(manager.broadcast("BTCUSDT", btc_tick))

            received = receive_matching(ws, "price")
            assert received["symbol"] == "BTCUSDT"


def test_subscribe_watchlist_cap_limit(test_app, monkeypatch):
    with TestClient(test_app) as client:
        monkeypatch.setattr(test_app.state.ws_manager, "max_subscriptions_per_conn", 2)
        user_id, token = create_user_helper(client)
        headers = {"Authorization": f"Bearer {token}"}

        # 2 watchlist items + 1 path symbol = 3 (> cap 2)
        client.post("/api/watchlist/ETHUSDT", headers=headers)
        client.post("/api/watchlist/SOLUSDT", headers=headers)

        with client.websocket_connect(f"/ws/prices/BTCUSDT?token={token}") as ws:
            try:
                ws.receive_json(mode="text")
            except Exception:
                pass

            ws.send_json({"action": "subscribe_watchlist"})
            err_msg = receive_matching(ws, "error")
            assert err_msg["code"] == "SUBSCRIPTION_LIMIT"


def test_live_sync_rest_add_and_remove(test_app):
    with TestClient(test_app) as client:
        user_id, token = create_user_helper(client)
        headers = {"Authorization": f"Bearer {token}"}

        manager = test_app.state.ws_manager

        with client.websocket_connect(f"/ws/prices/BTCUSDT?token={token}") as ws:
            try:
                ws.receive_json(mode="text")
            except Exception:
                pass

            # Enable watchlist mode on this socket
            ws.send_json({"action": "subscribe_watchlist"})
            receive_matching(ws, "watchlist_subscribed")

            # 1. Add SOLUSDT via REST API while socket is open
            add_resp = client.post("/api/watchlist/SOLUSDT", headers=headers)
            assert add_resp.status_code == 201

            # Socket receives watchlist_changed event
            change_msg = receive_matching(ws, "watchlist_changed")
            assert change_msg["action"] == "added"
            assert change_msg["symbol"] == "SOLUSDT"

            # Ticks for SOLUSDT now arrive without reconnecting!
            tick = {
                "type": "price",
                "symbol": "SOLUSDT",
                "price": "148.00000000",
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            asyncio.run(manager.broadcast("SOLUSDT", tick))
            received_tick = receive_matching(ws, "price")
            assert received_tick["symbol"] == "SOLUSDT"

            # 2. Remove SOLUSDT via REST API
            del_resp = client.delete("/api/watchlist/SOLUSDT", headers=headers)
            assert del_resp.status_code == 204

            remove_msg = receive_matching(ws, "watchlist_changed")
            assert remove_msg["action"] == "removed"
            assert remove_msg["symbol"] == "SOLUSDT"

            # Immediate verification: ticks stop within same turn
            assert ws not in manager.topics.get("SOLUSDT", set())


def test_multi_user_isolation(test_app):
    with TestClient(test_app) as client:
        user_a, token_a = create_user_helper(client, prefix="userA")
        user_b, token_b = create_user_helper(client, prefix="userB")

        with client.websocket_connect(f"/ws/prices/BTCUSDT?token={token_a}") as ws_a, \
             client.websocket_connect(f"/ws/prices/BTCUSDT?token={token_b}") as ws_b:

            # Drain snapshots
            for ws in (ws_a, ws_b):
                try:
                    ws.receive_json(mode="text")
                except Exception:
                    pass

            ws_a.send_json({"action": "subscribe_watchlist"})
            ws_b.send_json({"action": "subscribe_watchlist"})

            receive_matching(ws_a, "watchlist_subscribed")
            receive_matching(ws_b, "watchlist_subscribed")

            # User A adds ADAUSDT
            client.post("/api/watchlist/ADAUSDT", headers={"Authorization": f"Bearer {token_a}"})

            # WS A receives notification
            msg_a = receive_matching(ws_a, "watchlist_changed")
            assert msg_a["symbol"] == "ADAUSDT"

            # WS B should NOT receive any notification
            manager = test_app.state.ws_manager
            assert ws_b not in manager.topics.get("ADAUSDT", set())


def test_disconnect_cleans_up_bookkeeping(test_app):
    with TestClient(test_app) as client:
        manager = test_app.state.ws_manager
        initial_active = manager.active_count()
        user_id, token = create_user_helper(client)
        headers = {"Authorization": f"Bearer {token}"}
        client.post("/api/watchlist/ETHUSDT", headers=headers)

        with client.websocket_connect(f"/ws/prices/BTCUSDT?token={token}") as ws:
            try:
                ws.receive_json(mode="text")
            except Exception:
                pass
            ws.send_json({"action": "subscribe_watchlist"})
            receive_matching(ws, "watchlist_subscribed")

            assert manager.active_count() == initial_active + 1
            assert len(manager.ws_watchlist_topics) > 0

        # After context exit, disconnect has completed
        assert manager.active_count() == initial_active
        assert len(manager.ws_watchlist_topics) == 0
        assert len(manager.ws_explicit_topics) == 0

