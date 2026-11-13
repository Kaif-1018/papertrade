"""Unit tests for ConnectionManager (app/services/ws_manager.py).

Verifies:
- Connection and user tracking
- Idempotent disconnect (sync and async)
- Topic subscription and unsubscription
- Per-connection subscription cap enforcement (max 20)
- Concurrent broadcast fan-out
- Slow-client timeout eviction (2-second timeout protection)
- Failed-client eviction without affecting healthy subscribers
- Direct user-targeted messaging
- Active and topic counts
"""

import asyncio
import json
from typing import Any
import uuid

import pytest
from starlette.websockets import WebSocketState

from app.services.ws_manager import ConnectionManager


class MockWebSocket:
    """Mock WebSocket for unit testing ConnectionManager."""

    def __init__(
        self,
        delay: float = 0.0,
        fail_send: bool = False,
        client_state: WebSocketState = WebSocketState.CONNECTING,
    ) -> None:
        self.sent_messages: list[dict[str, Any]] = []
        self.delay = delay
        self.fail_send = fail_send
        self.client_state = client_state
        self.close_called = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def accept(self) -> None:
        self.client_state = WebSocketState.CONNECTED

    async def send_json(self, message: dict[str, Any]) -> None:
        if self.fail_send:
            raise ConnectionResetError("Mock client connection reset")
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        self.sent_messages.append(message)

    async def send_text(self, text: str) -> None:
        if self.fail_send:
            raise ConnectionResetError("Mock client connection reset")
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        self.sent_messages.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_called = True
        self.close_code = code
        self.close_reason = reason
        self.client_state = WebSocketState.DISCONNECTED


@pytest.fixture
def manager() -> ConnectionManager:
    return ConnectionManager(max_subscriptions_per_conn=20)


@pytest.mark.asyncio
async def test_connect_registers_connection_and_accepts(manager: ConnectionManager) -> None:
    user_id = uuid.uuid4()
    ws = MockWebSocket(client_state=WebSocketState.CONNECTING)

    await manager.connect(ws, user_id=user_id)

    assert ws.client_state == WebSocketState.CONNECTED
    assert manager.active_count() == 1
    assert ws in manager._active_connections
    assert ws in manager.user_connections[user_id]
    assert manager.ws_to_user[ws] == user_id
    assert manager.ws_to_topics[ws] == set()


@pytest.mark.asyncio
async def test_disconnect_idempotence(manager: ConnectionManager) -> None:
    user_id = uuid.uuid4()
    ws = MockWebSocket()
    await manager.connect(ws, user_id=user_id)
    manager.subscribe(ws, "BTCUSDT")

    assert manager.active_count() == 1
    assert "BTCUSDT" in manager.topics
    assert user_id in manager.user_connections

    # First disconnect (synchronous call)
    manager.disconnect(ws)
    assert manager.active_count() == 0
    assert "BTCUSDT" not in manager.topics
    assert user_id not in manager.user_connections
    assert ws not in manager.ws_to_topics
    assert ws not in manager.ws_to_user

    # Second disconnect (awaited call) - must not raise
    await manager.disconnect(ws)
    assert manager.active_count() == 0


@pytest.mark.asyncio
async def test_subscribe_and_unsubscribe(manager: ConnectionManager) -> None:
    ws = MockWebSocket()
    await manager.connect(ws)

    # Subscribing normalizes symbol to uppercase
    assert manager.subscribe(ws, "btcusdt") is True
    assert "BTCUSDT" in manager.topics
    assert ws in manager.topics["BTCUSDT"]
    assert "BTCUSDT" in manager.ws_to_topics[ws]
    assert manager.topic_counts() == {"BTCUSDT": 1}

    # Subscribe to second symbol
    assert manager.subscribe(ws, "ETHUSDT") is True
    assert manager.topic_counts() == {"BTCUSDT": 1, "ETHUSDT": 1}

    # Unsubscribe
    assert manager.unsubscribe(ws, "BTCUSDT") is True
    assert "BTCUSDT" not in manager.topics  # pruned when empty
    assert "BTCUSDT" not in manager.ws_to_topics[ws]
    assert manager.topic_counts() == {"ETHUSDT": 1}

    manager.disconnect(ws)
    assert manager.topic_counts() == {}


@pytest.mark.asyncio
async def test_subscription_cap_enforced(manager: ConnectionManager) -> None:
    ws = MockWebSocket()
    await manager.connect(ws)

    # Subscribe up to max_subscriptions_per_conn (20)
    for i in range(20):
        sym = f"SYM{i}USDT"
        assert manager.subscribe(ws, sym) is True

    assert len(manager.ws_to_topics[ws]) == 20

    # 21st subscription must return False
    assert manager.subscribe(ws, "OVERFLOWUSDT") is False
    assert len(manager.ws_to_topics[ws]) == 20
    assert "OVERFLOWUSDT" not in manager.topics

    # Re-subscribing to existing symbol should succeed without bumping count
    assert manager.subscribe(ws, "SYM0USDT") is True
    assert len(manager.ws_to_topics[ws]) == 20

    # After unsubscribing one, a new one can be subscribed
    manager.unsubscribe(ws, "SYM0USDT")
    assert len(manager.ws_to_topics[ws]) == 19
    assert manager.subscribe(ws, "NEWPAIRUSDT") is True
    assert len(manager.ws_to_topics[ws]) == 20


@pytest.mark.asyncio
async def test_broadcast_delivers_only_to_subscribers(manager: ConnectionManager) -> None:
    ws_btc = MockWebSocket()
    ws_eth = MockWebSocket()
    ws_both = MockWebSocket()

    await manager.connect(ws_btc)
    await manager.connect(ws_eth)
    await manager.connect(ws_both)

    manager.subscribe(ws_btc, "BTCUSDT")
    manager.subscribe(ws_eth, "ETHUSDT")
    manager.subscribe(ws_both, "BTCUSDT")
    manager.subscribe(ws_both, "ETHUSDT")

    msg = {"type": "price", "symbol": "BTCUSDT", "price": "67000.00"}
    count = await manager.broadcast("BTCUSDT", msg)

    assert count == 2
    assert len(ws_btc.sent_messages) == 1
    assert ws_btc.sent_messages[0] == msg
    assert len(ws_both.sent_messages) == 1
    assert ws_both.sent_messages[0] == msg
    assert len(ws_eth.sent_messages) == 0  # not subscribed to BTCUSDT


@pytest.mark.asyncio
async def test_broadcast_drops_failing_client_and_protects_healthy(
    manager: ConnectionManager,
) -> None:
    ws_good = MockWebSocket()
    ws_bad = MockWebSocket(fail_send=True)

    await manager.connect(ws_good)
    await manager.connect(ws_bad)

    manager.subscribe(ws_good, "SOLUSDT")
    manager.subscribe(ws_bad, "SOLUSDT")

    assert manager.active_count() == 2

    msg = {"type": "price", "symbol": "SOLUSDT", "price": "180.50"}
    await manager.broadcast("SOLUSDT", msg)

    # Healthy client received message
    assert len(ws_good.sent_messages) == 1
    assert ws_good.sent_messages[0] == msg

    # Bad client was evicted from manager
    assert manager.active_count() == 1
    assert ws_bad not in manager._active_connections
    assert ws_bad not in manager.topics.get("SOLUSDT", set())


@pytest.mark.asyncio
async def test_broadcast_drops_slow_client_on_timeout(manager: ConnectionManager) -> None:
    ws_fast = MockWebSocket()
    # Mock a client that hangs for 3.0s, while broadcast timeout is set to 0.1s in test
    ws_slow = MockWebSocket(delay=3.0)

    await manager.connect(ws_fast)
    await manager.connect(ws_slow)

    manager.subscribe(ws_fast, "BTCUSDT")
    manager.subscribe(ws_slow, "BTCUSDT")

    assert manager.active_count() == 2

    msg = {"type": "price", "symbol": "BTCUSDT", "price": "68000.00"}
    # Use 0.1s timeout to test fast in unit tests
    await manager.broadcast("BTCUSDT", msg, timeout=0.1)

    # Fast client received message
    assert len(ws_fast.sent_messages) == 1

    # Slow client timed out and was disconnected
    assert manager.active_count() == 1
    assert ws_slow not in manager._active_connections
    assert ws_slow not in manager.topics.get("BTCUSDT", set())


@pytest.mark.asyncio
async def test_send_to_user_delivers_to_all_user_connections(manager: ConnectionManager) -> None:
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    ws_a1 = MockWebSocket()
    ws_a2 = MockWebSocket()
    ws_b = MockWebSocket()

    await manager.connect(ws_a1, user_id=user_a)
    await manager.connect(ws_a2, user_id=user_a)
    await manager.connect(ws_b, user_id=user_b)

    msg = {"type": "order_filled", "symbol": "BTCUSDT"}
    sent = await manager.send_to_user(user_a, msg)

    assert sent == 2
    assert len(ws_a1.sent_messages) == 1
    assert len(ws_a2.sent_messages) == 1
    assert len(ws_b.sent_messages) == 0  # user_b received nothing

