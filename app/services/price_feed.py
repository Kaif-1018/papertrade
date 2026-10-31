"""Binance real-time WebSocket price feed consumer.

Ingests ticker streams from Binance's combined stream endpoint, parses each
message using a pure function, and updates the in-memory price registry.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json

import structlog
import websockets
from websockets.client import WebSocketClientProtocol

from app.core.price_registry import set_price

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
        # Port 9443 is frequently blocked by local firewalls/proxies;
        # standard 443 is Binance's primary port and identical stream.
        base = base.replace(":9443", "")
    if not base.endswith("/stream"):
        base = f"{base}/stream"

    streams = "/".join(f"{s.strip().lower()}@ticker" for s in symbols if s.strip())
    return f"{base}?streams={streams}"


class PriceFeedService:
    """Consumer for the Binance combined ticker WebSocket feed."""

    def __init__(self, url_base: str, symbols: list[str]) -> None:
        self.url_base = url_base
        self.symbols = symbols
        self.stats = FeedStats()
        self._running = False
        self._active_ws: WebSocketClientProtocol | None = None
        self._stats_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        """Main service loop. Connects and retries on errors every 5s."""
        self._running = True
        self._stats_task = asyncio.create_task(self._log_stats_loop())
        logger.info("price_feed_started", symbols=self.symbols)

        while self._running:
            try:
                await self._connect_and_consume()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("price_feed_connection_error", error=str(exc))
                if self._running:
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        """Gracefully stop the service and close active connections."""
        self._running = False
        if self._stats_task is not None:
            self._stats_task.cancel()
            try:
                await self._stats_task
            except asyncio.CancelledError:
                pass
            self._stats_task = None

        if self._active_ws is not None:
            await self._active_ws.close()
            self._active_ws = None

        logger.info("price_feed_stopped")

    async def _connect_and_consume(self) -> None:
        """Connect to Binance WebSocket and consume ticker ticks."""
        url = build_stream_url(self.url_base, self.symbols)
        logger.info("connecting_price_feed", url=url)

        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            self._active_ws = ws
            logger.info("price_feed_connected")

            async for message in ws:
                if not self._running:
                    break

                tick = parse_ticker_message(message)
                if tick is not None:
                    self.stats.messages_received += 1
                    self.stats.last_message_at = datetime.now(timezone.utc)
                    self.stats.symbols_seen.add(tick.symbol)
                    set_price(tick.symbol, tick.price, tick.event_time)
                else:
                    self.stats.messages_dropped += 1

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
