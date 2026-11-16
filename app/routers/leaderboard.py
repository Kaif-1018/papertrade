"""Leaderboard endpoint — GET /api/leaderboard.

Thin router: authenticates caller, validates parameters, delegates to LeaderboardService,
and returns the competition rankings response.
"""

from fastapi import APIRouter, Depends, Query, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.rate_limit import rate_limit
from app.core.redis import get_redis
from app.models.user import User
from app.repositories.leaderboard_repository import LeaderboardRepository
from app.schemas.leaderboard import LeaderboardResponse
from app.services.leaderboard_service import LeaderboardService

router = APIRouter(
    prefix="/api/leaderboard",
    tags=["leaderboard"],
    dependencies=[Depends(rate_limit("general_api", limit=120, window_seconds=60, key="user"))],
)


@router.get(
    "",
    response_model=LeaderboardResponse,
    status_code=status.HTTP_200_OK,
    summary="Get platform leaderboard rankings",
)
async def get_leaderboard(
    limit: int = Query(20, ge=1, le=100, description="Top N rankings to return (default 20, max 100)"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> LeaderboardResponse:
    """Retrieve the platform leaderboard with stampede-protected Redis caching."""
    repo = LeaderboardRepository(session)
    service = LeaderboardService(repository=repo, redis=redis)
    return await service.get_leaderboard(current_user_id=user.id, limit=limit)
