"""Health-check endpoint — verifies Postgres, Redis, and Price Feed status."""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.redis import get_redis

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return system and service health status.

    HTTP status is always 200.
    Overall status is 'ok' when all subsystems are healthy;
    returns 'degraded' if any service is down or if price feed is disconnected/stale >30s.
    """
    services: dict[str, Any] = {}

    # Postgres check
    try:
        await db.execute(text("SELECT 1"))
        services["postgres"] = "ok"
    except Exception:
        services["postgres"] = "error"

    # Redis check
    try:
        redis = get_redis()
        pong = await redis.ping()
        services["redis"] = "ok" if pong else "error"
    except Exception:
        services["redis"] = "error"

    # Price Feed check
    feed_service = getattr(request.app.state, "price_feed", None)
    feed_is_ok = True

    if feed_service is not None:
        feed_status = feed_service.status()
        now = datetime.now(timezone.utc)
        is_stale = False

        if feed_status.last_message_at is not None:
            idle_seconds = (now - feed_status.last_message_at).total_seconds()
            if idle_seconds > 30.0:
                is_stale = True
        elif feed_status.connected_since is not None:
            idle_seconds = (now - feed_status.connected_since).total_seconds()
            if idle_seconds > 30.0:
                is_stale = True
        else:
            # Connected or waiting for initial message
            is_stale = not feed_status.connected

        feed_is_ok = feed_status.connected and not is_stale

        feed_data = {
            "connected": feed_status.connected,
            "last_message_at": (
                feed_status.last_message_at.isoformat()
                if feed_status.last_message_at
                else None
            ),
            "reconnect_count": feed_status.reconnect_count,
            "current_backoff": feed_status.current_backoff,
            "symbols": feed_status.symbols,
            "connected_since": (
                feed_status.connected_since.isoformat()
                if feed_status.connected_since
                else None
            ),
        }
        services["price_feed"] = feed_data

    # Overall evaluation
    all_ok = (
        services.get("postgres") == "ok"
        and services.get("redis") == "ok"
        and feed_is_ok
    )
    overall = "ok" if all_ok else "degraded"

    return {
        "status": overall,
        "services": services,
    }
