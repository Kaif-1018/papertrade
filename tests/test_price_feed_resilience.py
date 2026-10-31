"""Day 5 — Production-grade price feed resilience & health test suite.

Tests reconnection with exponential backoff & full jitter, silent stall watchdog,
Binance failure recovery, dynamic symbol subscribe/unsubscribe, and /health reporting.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest
import pytest_asyncio
from websockets.exceptions import ConnectionClosedOK

from app.core.price_registry import clear, get_price, is_supported, set_price
from app.services.price_feed import FeedStatus, PriceFeedService


@pytest.fixture(autouse=True)
def clean_registry():
    """Wipe price registry before and after every test."""
    clear()
    yield
    clear()


# ---------------------------------------------------------------------------
# 1. Normal Flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normal_feed_flow(mock_binance):
    """Client connects, ingests ticker ticks from server, and updates registry."""
    service = PriceFeedService(
        url_base=mock_binance.ws_url,
        symbols=["BTCUSDT"],
        watchdog_seconds=10.0,
    )
    task = asyncio.create_task(service.run())

    try:
        await mock_binance.wait_for_connection()
        await mock_binance.broadcast_tick("BTCUSDT", "68500.50000000")

        # Allow event loop to process incoming frame
        await asyncio.sleep(0.05)

        entry = get_price("BTCUSDT")
        assert entry.price == Decimal("68500.50000000")
        assert service.status().connected is True
        assert service.status().last_message_at is not None
    finally:
        await service.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# 2. Server Disconnect & Reconnect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_server_drop_and_reconnect(mock_binance):
    """When server drops connection, client reconnects, increments counter, and resumes ticks."""
    sleep_calls = []

    async def fast_sleep(d: float):
        sleep_calls.append(d)
        await asyncio.sleep(0.01)

    service = PriceFeedService(
        url_base=mock_binance.ws_url,
        symbols=["BTCUSDT"],
        sleep_fn=fast_sleep,
        random_fn=lambda a, b: 0.01,
        watchdog_seconds=10.0,
    )
    task = asyncio.create_task(service.run())

    try:
        await mock_binance.wait_for_connection()
        await mock_binance.broadcast_tick("BTCUSDT", "67000.00")
        await asyncio.sleep(0.05)
        assert get_price("BTCUSDT").price == Decimal("67000.00")

        # Drop connections from server
        await mock_binance.drop_connections(code=1001, reason="Server reboot")
        await asyncio.sleep(0.05)

        # Wait for client to reconnect
        await mock_binance.wait_for_connection()
        assert service.status().reconnect_count >= 1

        # Broadcast new tick after reconnect
        await mock_binance.broadcast_tick("BTCUSDT", "67500.00")
        await asyncio.sleep(0.05)
        assert get_price("BTCUSDT").price == Decimal("67500.00")
    finally:
        await service.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# 3. Exponential Backoff & Healthy Reset
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backoff_growth_and_healthy_reset():
    """Backoff sequence doubles and caps at max_backoff, resetting only after healthy duration."""
    recorded_sleeps = []

    async def mock_sleep(d: float):
        recorded_sleeps.append(d)
        await asyncio.sleep(0.001)

    # Use deterministic random_fn that returns current_backoff exactly
    service = PriceFeedService(
        url_base="ws://127.0.0.1:9999",  # unreachable port
        symbols=["BTCUSDT"],
        min_backoff=1.0,
        max_backoff=60.0,
        healthy_threshold_seconds=0.05,
        sleep_fn=mock_sleep,
        random_fn=lambda low, high: high,  # return full ceiling
    )

    # Simulate backoff progression
    assert service._current_backoff == 1.0

    d1 = service._compute_backoff()
    assert d1 == 1.0
    service._current_backoff = min(60.0, service._current_backoff * 2.0)
    assert service._current_backoff == 2.0

    d2 = service._compute_backoff()
    assert d2 == 2.0
    service._current_backoff = min(60.0, service._current_backoff * 2.0)
    assert service._current_backoff == 4.0

    # Advance until cap
    for _ in range(10):
        service._current_backoff = min(60.0, service._current_backoff * 2.0)
    assert service._current_backoff == 60.0
    assert service._compute_backoff() == 60.0

    # Test healthy timer resets to min_backoff
    healthy_task = asyncio.create_task(service._healthy_timer())
    await healthy_task
    assert service._current_backoff == 1.0


# ---------------------------------------------------------------------------
# 4. Watchdog Silent Stall Detection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watchdog_silent_stall(mock_binance):
    """When connection remains open but completely silent, watchdog force-reconnects."""
    async def fast_sleep(d: float):
        await asyncio.sleep(min(d, 0.05))

    service = PriceFeedService(
        url_base=mock_binance.ws_url,
        symbols=["BTCUSDT"],
        watchdog_seconds=0.15,  # 150ms timeout
        sleep_fn=fast_sleep,
        random_fn=lambda a, b: 0.01,
    )
    task = asyncio.create_task(service.run())

    try:
        await mock_binance.wait_for_connection()
        initial_reconnects = service.status().reconnect_count

        # Keep server silent for > watchdog_seconds
        await asyncio.sleep(0.35)

        # Watchdog must have triggered a reconnect
        assert service.status().reconnect_count > initial_reconnects
    finally:
        await service.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# 5. Garbage Messages Handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_garbage_messages_do_not_break_feed(mock_binance):
    """Malformed or non-ticker messages are dropped without stopping valid tick ingestion."""
    service = PriceFeedService(
        url_base=mock_binance.ws_url,
        symbols=["BTCUSDT", "ETHUSDT"],
        watchdog_seconds=10.0,
    )
    task = asyncio.create_task(service.run())

    try:
        await mock_binance.wait_for_connection()

        # 1. Valid tick
        await mock_binance.broadcast_tick("BTCUSDT", "68000.00")
        await asyncio.sleep(0.02)
        assert get_price("BTCUSDT").price == Decimal("68000.00")

        # 2. Garbage payloads
        await mock_binance.broadcast("not-a-valid-json {{{{")
        await mock_binance.broadcast(json.dumps({"stream": "unknown@depth"}))
        await mock_binance.broadcast(
            json.dumps({"data": {"s": "BTCUSDT", "c": "-10.0", "E": 12345}})
        )
        await asyncio.sleep(0.02)

        # 3. Another valid tick
        await mock_binance.broadcast_tick("ETHUSDT", "3500.00")
        await asyncio.sleep(0.02)
        assert get_price("ETHUSDT").price == Decimal("3500.00")

        # Ensure dropped messages were counted and service is still connected
        assert service.stats.messages_dropped >= 3
        assert service.status().connected is True
    finally:
        await service.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# 6. Dynamic Symbol Subscriptions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dynamic_add_and_remove_symbols(mock_binance):
    """add_symbol and remove_symbol send SUBSCRIBE/UNSUBSCRIBE and update desired set."""
    service = PriceFeedService(
        url_base=mock_binance.ws_url,
        symbols=["BTCUSDT"],
        watchdog_seconds=10.0,
    )
    task = asyncio.create_task(service.run())

    try:
        await mock_binance.wait_for_connection()

        # Seed BTCUSDT price
        await mock_binance.broadcast_tick("BTCUSDT", "68000.00")
        await asyncio.sleep(0.02)
        assert get_price("BTCUSDT").price == Decimal("68000.00")

        # Add SOLUSDT dynamically
        await service.add_symbol("SOLUSDT")
        await asyncio.sleep(0.05)
        assert "SOLUSDT" in service.status().symbols

        # Verify server received SUBSCRIBE frame
        sub_msgs = [
            json.loads(m)
            for m in mock_binance.received_messages
            if "SUBSCRIBE" in m
        ]
        assert any(
            m.get("method") == "SUBSCRIBE" and "solusdt@ticker" in m.get("params", [])
            for m in sub_msgs
        )

        # Remove BTCUSDT dynamically
        await service.remove_symbol("BTCUSDT")
        await asyncio.sleep(0.05)
        assert "BTCUSDT" not in service.status().symbols

        # Verify removed from registry
        with pytest.raises(Exception):
            get_price("BTCUSDT")

        # Verify server received UNSUBSCRIBE frame
        unsub_msgs = [
            json.loads(m)
            for m in mock_binance.received_messages
            if "UNSUBSCRIBE" in m
        ]
        assert any(
            m.get("method") == "UNSUBSCRIBE" and "btcusdt@ticker" in m.get("params", [])
            for m in unsub_msgs
        )
    finally:
        await service.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# 7. Prompt Shutdown During Backoff Sleep
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shutdown_during_backoff_sleep_exits_promptly():
    """Stopping the service during a long backoff sleep cancels cleanly without hanging."""
    service = PriceFeedService(
        url_base="ws://127.0.0.1:9999",  # unreachable port
        symbols=["BTCUSDT"],
        min_backoff=100.0,  # 100 seconds
        max_backoff=100.0,
        random_fn=lambda low, high: 100.0,
    )
    task = asyncio.create_task(service.run())

    # Wait for first connection failure and backoff sleep entry
    await asyncio.sleep(0.05)
    assert task.done() is False

    # Stop service and cancel task
    await service.stop()
    task.cancel()

    # Must exit promptly without hanging
    t0 = datetime.now(timezone.utc)
    try:
        await asyncio.wait_for(task, timeout=0.5)
    except asyncio.CancelledError:
        pass
    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    assert elapsed < 0.5


# ---------------------------------------------------------------------------
# 8. Health Endpoint Reporting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_endpoint_price_feed_status(client, app_instance):
    """GET /health reports price_feed metadata and marks 'degraded' when feed is disconnected."""
    # 1. Healthy feed scenario
    mock_feed = PriceFeedService(
        url_base="ws://127.0.0.1:9999",
        symbols=["BTCUSDT"],
    )
    mock_feed._connected = True
    now = datetime.now(timezone.utc)
    mock_feed._connected_since = now
    mock_feed._last_message_at = now

    app_instance.state.price_feed = mock_feed

    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "price_feed" in data["services"]
    assert data["services"]["price_feed"]["connected"] is True

    # 2. Disconnected feed scenario -> overall 'degraded', HTTP stays 200
    mock_feed._connected = False
    mock_feed._last_message_at = now - timedelta(seconds=45)

    resp_degraded = await client.get("/health")
    assert resp_degraded.status_code == 200
    deg_data = resp_degraded.json()
    assert deg_data["status"] == "degraded"
    assert deg_data["services"]["price_feed"]["connected"] is False

