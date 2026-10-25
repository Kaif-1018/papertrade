"""Health-check endpoint — verifies Postgres and Redis connectivity."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.redis import get_redis

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)) -> dict:
    """Return per-service status. Overall is 'ok' only when all services are up."""
    status: dict[str, str] = {}

    # Postgres
    try:
        await db.execute(text("SELECT 1"))
        status["postgres"] = "ok"
    except Exception:
        status["postgres"] = "error"

    # Redis
    try:
        redis = get_redis()
        pong = await redis.ping()
        status["redis"] = "ok" if pong else "error"
    except Exception:
        status["redis"] = "error"

    overall = "ok" if all(v == "ok" for v in status.values()) else "degraded"
    return {"status": overall, "services": status}
