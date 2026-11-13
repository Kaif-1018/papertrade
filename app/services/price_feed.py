"""Binance real-time WebSocket price feed consumer.

Ingests ticker streams from Binance's combined stream endpoint, parses each
message using a pure function, and updates the in-memory price registry.
Features:
- Exponential backoff with full jitter and healthy-reset timer
- Keepalive and silent stall watchdog detection
- Robust failure handling and 24h connection rollover support
- Dynamic SUBSCRIBE/UNSUBSCRIBE with desired-symbols synchronization
- Observable FeedStatus metrics
"""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import random
from typing import Any

import structlog
import websockets
from websockets.client import WebSocketClientProtocol
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
)

from app.core.price_registry import remove_price, set_price
from app.core.redis import get_redis

logger = structlog.stdlib.get_logger(__name__)


@dataclass(frozen=True)
class ParsedTick:
    """Validated price tick extracted from a ticker message."""

    symbol: str
    price: Decimal
    event_time: datetime


@dataclass
class FeedStats:
    """Internal metrics for monitoring message ingestion."""

    messages_received: int = 0
    messages_dropped: int = 0
    last_message_at: datetime | None = None
    symbols_seen: set[str] = field(default_factory=set)


@dataclass
class FeedStatus:
    """Public diagnostic status of the price feed service."""

    connected: bool
    last_message_at: datetime | None
    reconnect_count: int
    current_backoff: float
    symbols: list[str]
    connected_since: datetime | None


def parse_ticker_message(raw: str | bytes) -> ParsedTick | None:
    """Pure parsing function for Binance combined stream ticker messages.

    Expected format:
      {"stream": "btcusdt@ticker", "data": {"s": "BTCUSDT", "c": "67500.50", "E": 1710000000000}}

    Never raises an exception; returns None for any malformed or unexpected data.
    """
    try:
        if isinstance(raw, bytes):
            raw_str = raw.decode("utf-8")
        elif isinstance(raw, str):
            raw_str = raw
        else:
            logger.debug("parse_ticker_invalid_type", raw_type=type(raw).__name__)
            return None

        payload = json.loads(raw_str)
        if not isinstance(payload, dict):
            logger.debug("parse_ticker_payload_not_dict")
            return None

        # Support combined stream format (`data` field) or direct ticker payload
        data = payload.get("data")
        if data is None and "s" in payload and "c" in payload:
            data = payload
        elif not isinstance(data, dict):
            logger.debug("parse_ticker_no_data_field")
            return None

        # Extract required fields: s (symbol), c (price string), E (epoch ms)
        if "s" not in data or "c" not in data or "E" not in data:
            logger.debug("parse_ticker_missing_fields")
            return None

        raw_symbol = data["s"]
        raw_price = data["c"]
        raw_event_time = data["E"]

        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            logger.debug("parse_ticker_invalid_symbol")
            return None

        # Price must be parsed as Decimal directly from string — NEVER float
        if not isinstance(raw_price, str):
            logger.debug("parse_ticker_price_not_str")
            return None

        try:
            price = Decimal(raw_price)
        except (InvalidOperation, TypeError, ValueError):
            logger.debug("parse_ticker_unparsable_price", price=raw_price)
            return None

        # Reject non-positive prices
        if price <= Decimal("0"):
            logger.debug("parse_ticker_non_positive_price", price=str(price))
            return None

        # Event time validation (epoch milliseconds)
        if not isinstance(raw_event_time, (int, float)) or raw_event_time < 0:
            logger.debug("parse_ticker_invalid_event_time", event_time=raw_event_time)
            return None

        event_time = datetime.fromtimestamp(raw_event_time / 1000.0, tz=timezone.utc)
        symbol = raw_symbol.strip().upper()

        return ParsedTick(
            symbol=symbol,
            price=price,
            event_time=event_time,
        )
    except Exception as exc:
        logger.debug("parse_ticker_exception", error=str(exc))
        return None


def build_stream_url(url_base: str, symbols: list[str]) -> str:
    """Construct Binance combined stream URL for the provided symbols."""
    base = url_base.rstrip("/")
    if base.endswith("/ws"):
        base = base[:-3]
    if ":9443" in base:
        base = base.replace(":9443", "")
    if not base.endswith("/stream"):
        base = f"{base}/stream"

    valid_symbols = [s.strip().lower() for s in symbols if s.strip()]
    if not valid_symbols:
        return base

    streams = "/".join(f"{s}@ticker" for s in valid_symbols)
    return f"{base}?streams={streams}"


class PriceFeedService:
    """Production-grade consumer for Binance WebSocket ticker feeds."""

    def __init__(
        self,
        url_base: str,
        symbols: list[str],
        watchdog_seconds: float = 15.0,
        min_backoff: float = 1.0,
        max_backoff: float = 60.0,
        healthy_threshold_seconds: float = 30.0,
        sleep_fn: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
        random_fn: Callable[[float, float], float] = random.uniform,
        time_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        redis: Any | None = None,
    ) -> None:
        self.url_base = url_base
        self._symbols: set[str] = {s.strip().upper() for s in symbols if s.strip()}
        self.watchdog_seconds = watchdog_seconds
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff
        self._healthy_threshold_seconds = healthy_threshold_seconds
        self._sleep_fn = sleep_fn
        self._random_fn = random_fn
        self._time_fn = time_fn
        self._redis = redis
        self._last_redis_err_log_at: float = -100.0

        self.stats = FeedStats()
        self._running = False
        self._connected = False
        self._connected_since: datetime | None = None
        self._last_message_at: datetime | None = None
        self._reconnect_count = 0
        self._current_backoff = min_backoff

        self._active_ws: WebSocketClientProtocol | None = None
        self._sub_lock = asyncio.Lock()
        self._req_id = 0
        self._stats_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        """Main resilient loop: connects, consumes, and backs off on disconnects."""
        self._running = True
        self._stats_task = asyncio.create_task(self._log_stats_loop())
        logger.info("price_feed_started", symbols=sorted(self._symbols))

        while self._running:
            disconnect_reason = "Initial connection"
            try:
                disconnect_reason = await self._connect_once()
            except asyncio.CancelledError:
                logger.info("price_feed_run_cancelled")
                raise
            except Exception as exc:
                disconnect_reason = f"{type(exc).__name__}: {exc}"

            if not self._running:
                break

            self._reconnect_count += 1
            delay = self._compute_backoff()
            logger.info(
                "reconnecting_price_feed",
                attempt=self._reconnect_count,
                delay=f"{delay:.2f}",
                reason=disconnect_reason,
                current_backoff=self._current_backoff,
            )

            # Advance backoff for subsequent failures (capped at max_backoff)
            self._current_backoff = min(
                self._max_backoff, self._current_backoff * 2.0
            )

            try:
                await self._sleep_fn(delay)
            except asyncio.CancelledError:
                raise

    async def stop(self) -> None:
        """Gracefully terminate service loop, active WebSocket, and child tasks."""
        self._running = False
        if self._stats_task is not None and not self._stats_task.done():
            self._stats_task.cancel()
            try:
                await self._stats_task
            except asyncio.CancelledError:
                pass
            self._stats_task = None

        if self._active_ws is not None and not self._active_ws.closed:
            await self._active_ws.close()
            self._active_ws = None

        logger.info("price_feed_stopped")

    def status(self) -> FeedStatus:
        """Return diagnostic health metrics of the feed service."""
        return FeedStatus(
            connected=self._connected,
            last_message_at=self._last_message_at,
            reconnect_count=self._reconnect_count,
            current_backoff=self._current_backoff,
            symbols=sorted(self._symbols),
            connected_since=self._connected_since,
        )

    def _compute_backoff(self) -> float:
        """Compute delay using Full Jitter: Uniform(0, current_backoff)."""
        return self._random_fn(0.0, self._current_backoff)

    async def _connect_once(self) -> str:
        """Establish a single WebSocket connection and consume until disconnection.

        Spawns and cancels watchdog and healthy-reset helper tasks.
        """
        async with self._sub_lock:
            current_symbols = sorted(self._symbols)

        url = build_stream_url(self.url_base, current_symbols)
        reason = "Normal connection termination"
        watchdog_task: asyncio.Task[None] | None = None
        healthy_task: asyncio.Task[None] | None = None

        try:
            logger.info("connecting_price_feed", url=url, symbol_count=len(current_symbols))
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            ) as ws:
                self._active_ws = ws
                self._connected = True
                self._connected_since = self._time_fn()
                logger.info("price_feed_connected", symbols=current_symbols)

                # Watchdog catches silent stalls; healthy timer resets backoff after 30s
                watchdog_task = asyncio.create_task(self._watchdog(ws))
                healthy_task = asyncio.create_task(self._healthy_timer())

                try:
                    async for raw_message in ws:
                        if not self._running:
                            break
                        await self._handle_message(raw_message)
                except ConnectionClosedOK as exc:
                    reason = f"Connection closed normally ({exc.code}): {exc.reason}"
                    logger.info("price_feed_connection_closed_ok", reason=reason)
                except ConnectionClosedError as exc:
                    # Binance 24h disconnects frequently use 1000/1001 with descriptive reason
                    if exc.code == 1000 or "24h" in str(exc.reason).lower():
                        reason = f"Normal rollover ({exc.code}): {exc.reason}"
                        logger.info("price_feed_rollover", reason=reason)
                    else:
                        reason = f"Connection closed with error ({exc.code}): {exc.reason}"
                        logger.warning("price_feed_connection_closed_error", reason=reason)

        except (ConnectionClosed, OSError, TimeoutError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning("price_feed_network_error", error=reason)
        finally:
            self._connected = False
            self._active_ws = None

            # Always clean up child tasks to guarantee no task leaks across reconnects
            for t in (watchdog_task, healthy_task):
                if t is not None and not t.done():
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass

        return reason

    async def _watchdog(self, ws: WebSocketClientProtocol) -> None:
        """Periodically checks message recency; force-closes connection on silent stall."""
        check_interval = max(0.5, self.watchdog_seconds / 3.0)
        try:
            while self._connected:
                await self._sleep_fn(check_interval)
                now = self._time_fn()
                last_activity = self._last_message_at or self._connected_since
                if last_activity is not None:
                    idle = (now - last_activity).total_seconds()
                    if idle >= self.watchdog_seconds:
                        logger.warning(
                            "price_feed_stall_detected",
                            idle_seconds=f"{idle:.1f}",
                            threshold=self.watchdog_seconds,
                        )
                        await ws.close(code=1000, reason="Watchdog timeout - silent stall detected")
                        break
        except asyncio.CancelledError:
            pass

    async def _healthy_timer(self) -> None:
        """Resets backoff to min_backoff once connection survives healthy_threshold_seconds."""
        try:
            await self._sleep_fn(self._healthy_threshold_seconds)
            self._current_backoff = self._min_backoff
            logger.info("price_feed_backoff_reset_healthy", current_backoff=self._min_backoff)
        except asyncio.CancelledError:
            pass

    async def _handle_message(self, raw: str | bytes) -> None:
        """Process incoming raw WebSocket message."""
        now = self._time_fn()
        self._last_message_at = now
        self.stats.last_message_at = now

        # Ignore Binance subscription acks: {"result": null, "id": 1}
        try:
            text = raw if isinstance(raw, str) else raw.decode("utf-8")
            if '"result"' in text and '"data"' not in text:
                data = json.loads(text)
                if data.get("result") is None and "id" in data:
                    logger.debug("price_feed_subscription_ack", ack_id=data.get("id"))
                    return
        except Exception:
            pass

        tick = parse_ticker_message(raw)
        if tick is not None:
            self.stats.messages_received += 1
            self.stats.symbols_seen.add(tick.symbol)
            set_price(tick.symbol, tick.price, tick.event_time)
            await self._publish_tick(tick)
        else:
            self.stats.messages_dropped += 1

    async def _publish_tick(self, tick: ParsedTick) -> None:
        """Publish parsed tick to Redis pubsub channel prices:{SYMBOL}.

        A Redis failure must never break or slow down the feed loop.
        Failures are caught and logged at warning level with rate limiting (max once per 10s).
        """
        try:
            r = self._redis or get_redis()
            payload = json.dumps({
                "type": "price",
                "symbol": tick.symbol,
                "price": str(tick.price),
                "ts": tick.event_time.isoformat(),
            })
            await r.publish(f"prices:{tick.symbol}", payload)
        except Exception as exc:
            now = asyncio.get_event_loop().time()
            if now - self._last_redis_err_log_at >= 10.0:
                self._last_redis_err_log_at = now
                logger.warning("price_feed_redis_publish_error", error=str(exc))

    async def add_symbol(self, symbol: str) -> None:
        """Dynamically subscribe to a new symbol without dropping connection."""
        sym = symbol.strip().upper()
        async with self._sub_lock:
            if sym in self._symbols:
                return
            self._symbols.add(sym)
            ws = self._active_ws

            if ws is not None and not ws.closed:
                self._req_id += 1
                msg = {
                    "method": "SUBSCRIBE",
                    "params": [f"{sym.lower()}@ticker"],
                    "id": self._req_id,
                }
                try:
                    await ws.send(json.dumps(msg))
                    logger.info("price_feed_subscribe_sent", symbol=sym, id=self._req_id)
                except Exception as exc:
                    logger.warning("price_feed_subscribe_failed", symbol=sym, error=str(exc))

    async def remove_symbol(self, symbol: str) -> None:
        """Dynamically unsubscribe from a symbol and remove from price registry."""
        sym = symbol.strip().upper()
        async with self._sub_lock:
            if sym in self._symbols:
                self._symbols.remove(sym)

            # Evict from price registry immediately
            remove_price(sym)
            ws = self._active_ws

            if ws is not None and not ws.closed:
                self._req_id += 1
                msg = {
                    "method": "UNSUBSCRIBE",
                    "params": [f"{sym.lower()}@ticker"],
                    "id": self._req_id,
                }
                try:
                    await ws.send(json.dumps(msg))
                    logger.info("price_feed_unsubscribe_sent", symbol=sym, id=self._req_id)
                except Exception as exc:
                    logger.warning("price_feed_unsubscribe_failed", symbol=sym, error=str(exc))

    async def _log_stats_loop(self) -> None:
        """Periodic background task logging ingestion rate every 30 seconds."""
        last_count = self.stats.messages_received
        while self._running:
            try:
                await asyncio.sleep(30)
                current_count = self.stats.messages_received
                rate = (current_count - last_count) / 30.0
                last_count = current_count
                logger.info(
                    "price_feed_metrics",
                    msgs_per_sec=f"{rate:.1f}",
                    symbols_seen=len(self.stats.symbols_seen),
                    total_received=self.stats.messages_received,
                    total_dropped=self.stats.messages_dropped,
                )
            except asyncio.CancelledError:
                break
