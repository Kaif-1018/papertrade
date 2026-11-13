"""Redis pubsub listener for real-time price tick broadcasting.

Listens to `prices:*` channels on Redis and fans incoming ticks out
to subscribed WebSocket clients via ConnectionManager.
"""

import asyncio
import json
from typing import Any

from redis.asyncio import Redis
import structlog

from app.core.redis import get_redis
from app.services.ws_manager import ConnectionManager

logger = structlog.stdlib.get_logger(__name__)


class PriceBroadcasterService:
    """Consumes price tick messages from Redis pubsub and dispatches to ConnectionManager."""

    def __init__(
        self,
        manager: ConnectionManager,
        redis: Redis | None = None,
    ) -> None:
        self.manager = manager
        self._redis = redis
        self._running = False

    async def run(self) -> None:
        """Main loop: psubscribes to prices:* and fans messages out."""
        self._running = True
        logger.info("price_broadcaster_started")

        while self._running:
            pubsub = None
            try:
                r = self._redis or get_redis()
                pubsub = r.pubsub()
                await pubsub.psubscribe("prices:*")

                async for message in pubsub.listen():
                    if not self._running:
                        break

                    if message.get("type") != "pmessage":
                        continue

                    raw_channel = message.get("channel")
                    channel = (
                        raw_channel.decode("utf-8")
                        if isinstance(raw_channel, bytes)
                        else str(raw_channel)
                    )

                    # Channel format: prices:{SYMBOL}
                    if ":" not in channel:
                        continue
                    symbol = channel.split(":", 1)[1].upper()

                    # Skip entirely if there are no active subscribers for this topic
                    if not self.manager.topic_counts().get(symbol, 0):
                        continue

                    raw_data = message.get("data")
                    if raw_data is None:
                        continue

                    text_data = (
                        raw_data.decode("utf-8")
                        if isinstance(raw_data, bytes)
                        else str(raw_data)
                    )

                    try:
                        payload = json.loads(text_data)
                    except Exception:
                        continue

                    await self.manager.broadcast(symbol, payload)

            except asyncio.CancelledError:
                logger.info("price_broadcaster_cancelled")
                break
            except Exception as exc:
                logger.warning("price_broadcaster_reconnecting", error=str(exc))
                try:
                    await asyncio.sleep(1.0)
                except asyncio.CancelledError:
                    break
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.punsubscribe("prices:*")
                        if hasattr(pubsub, "aclose"):
                            await pubsub.aclose()
                        else:
                            res = pubsub.close()
                            if asyncio.iscoroutine(res):
                                await res
                    except Exception:
                        pass

        logger.info("price_broadcaster_stopped")

    async def stop(self) -> None:
        """Stop the broadcaster loop."""
        self._running = False
