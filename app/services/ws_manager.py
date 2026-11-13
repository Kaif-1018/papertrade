"""WebSocket connection and topic subscription manager.

Maintains active WebSocket connections, topic subscription mappings,
per-user mappings, and handles concurrent fan-out broadcasting with
slow-client protection (2-second send timeout) and automatic eviction.
"""

import asyncio
from collections.abc import Generator
import json
from typing import Any
from uuid import UUID

from fastapi import WebSocket
import structlog

logger = structlog.stdlib.get_logger(__name__)


class _DoneAwaitable:
    """Awaitable helper allowing disconnect() to be called with or without await."""

    def __await__(self) -> Generator[None, None, None]:
        if False:
            yield
        return None


class SubscriptionLimitError(Exception):
    """Raised when a client exceeds the per-connection subscription cap."""

    pass


class ConnectionManager:
    """Manages active WebSockets and topic-based pubsub fan-out."""

    def __init__(self, max_subscriptions_per_conn: int = 20) -> None:
        # Topic -> set of WebSockets
        self.topics: dict[str, set[Any]] = {}
        # User ID -> set of WebSockets for targeted notifications
        self.user_connections: dict[UUID, set[Any]] = {}
        # Reverse map: WebSocket -> set of subscribed topics (O(1) disconnect cleanup)
        self.ws_to_topics: dict[Any, set[str]] = {}
        # Reverse map: WebSocket -> User ID
        self.ws_to_user: dict[Any, UUID | None] = {}
        # Set of all active connections
        self._active_connections: set[Any] = set()
        # Per-connection topic subscription cap
        self.max_subscriptions_per_conn = max_subscriptions_per_conn

    async def connect(self, ws: Any, user_id: UUID | None = None) -> None:
        """Register a new WebSocket connection."""
        # Accept if Starlette WebSocket is in CONNECTING state
        if hasattr(ws, "client_state"):
            from starlette.websockets import WebSocketState

            if ws.client_state == WebSocketState.CONNECTING:
                await ws.accept()
        elif hasattr(ws, "accept"):
            try:
                res = ws.accept()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass

        self._active_connections.add(ws)
        self.ws_to_topics[ws] = set()
        self.ws_to_user[ws] = user_id

        if user_id is not None:
            self.user_connections.setdefault(user_id, set()).add(ws)

        logger.debug("ws_connected", extra={"user_id": str(user_id) if user_id else None})

    def disconnect(self, ws: Any) -> Any:
        """Idempotently unregister a WebSocket and remove from all topics and user maps."""
        # Remove from topics
        subscribed_topics = self.ws_to_topics.pop(ws, None)
        if subscribed_topics:
            for topic in subscribed_topics:
                subscribers = self.topics.get(topic)
                if subscribers is not None:
                    subscribers.discard(ws)
                    if not subscribers:
                        self.topics.pop(topic, None)

        # Remove from user connections
        user_id = self.ws_to_user.pop(ws, None)
        if user_id is not None:
            user_conns = self.user_connections.get(user_id)
            if user_conns is not None:
                user_conns.discard(ws)
                if not user_conns:
                    self.user_connections.pop(user_id, None)

        self._active_connections.discard(ws)

        # Attempt to close if socket is still open
        try:
            if hasattr(ws, "close"):
                res = ws.close()
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
        except Exception:
            pass

        logger.debug("ws_disconnected", extra={"user_id": str(user_id) if user_id else None})
        return _DoneAwaitable()

    def subscribe(self, ws: Any, symbol: str) -> bool:
        """Subscribe *ws* to *symbol*.

        Returns False if the client has reached the per-connection subscription cap.
        """
        sym = symbol.strip().upper()
        current_topics = self.ws_to_topics.setdefault(ws, set())

        if sym not in current_topics and len(current_topics) >= self.max_subscriptions_per_conn:
            return False

        self.topics.setdefault(sym, set()).add(ws)
        current_topics.add(sym)
        return True

    def unsubscribe(self, ws: Any, symbol: str) -> bool:
        """Unsubscribe *ws* from *symbol*."""
        sym = symbol.strip().upper()

        subscribers = self.topics.get(sym)
        if subscribers is not None:
            subscribers.discard(ws)
            if not subscribers:
                self.topics.pop(sym, None)

        if ws in self.ws_to_topics:
            self.ws_to_topics[ws].discard(sym)

        return True

    async def broadcast(self, symbol: str, message: dict, timeout: float = 2.0) -> int:
        """Fan out *message* concurrently to all subscribers of *symbol*.

        Iterates over a shallow COPY of the subscribers set.
        Clients that fail or exceed *timeout* (default 2s) are disconnected
        and pruned immediately to protect against slow-client backpressure.
        """
        sym = symbol.strip().upper()
        subscribers = list(self.topics.get(sym, set()))
        if not subscribers:
            return 0

        json_str = json.dumps(message)

        async def _send(ws: Any) -> None:
            try:
                if hasattr(ws, "send_json"):
                    await asyncio.wait_for(ws.send_json(message), timeout=timeout)
                elif hasattr(ws, "send_text"):
                    await asyncio.wait_for(ws.send_text(json_str), timeout=timeout)
            except Exception as exc:
                logger.warning(
                    "ws_broadcast_client_dropped",
                    extra={"symbol": sym, "error": str(exc)},
                )
                self.disconnect(ws)

        await asyncio.gather(*(_send(ws) for ws in subscribers), return_exceptions=True)
        return len(subscribers)

    async def send_to_user(self, user_id: UUID, message: dict, timeout: float = 2.0) -> int:
        """Send *message* directly to all active WebSockets belonging to *user_id*."""
        conns = list(self.user_connections.get(user_id, set()))
        if not conns:
            return 0

        json_str = json.dumps(message)

        async def _send(ws: Any) -> None:
            try:
                if hasattr(ws, "send_json"):
                    await asyncio.wait_for(ws.send_json(message), timeout=timeout)
                elif hasattr(ws, "send_text"):
                    await asyncio.wait_for(ws.send_text(json_str), timeout=timeout)
            except Exception:
                self.disconnect(ws)

        await asyncio.gather(*(_send(ws) for ws in conns), return_exceptions=True)
        return len(conns)

    def active_count(self) -> int:
        """Return total active WebSocket connections."""
        return len(self._active_connections)

    def topic_counts(self) -> dict[str, int]:
        """Return a mapping of symbol -> count of active subscribers."""
        return {sym: len(subs) for sym, subs in self.topics.items() if subs}


def get_ws_manager(websocket: WebSocket) -> ConnectionManager:
    """FastAPI dependency to retrieve the singleton ConnectionManager from app.state."""
    return websocket.app.state.ws_manager
