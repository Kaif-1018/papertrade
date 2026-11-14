"""Analytics router: GET /api/analytics/pnl."""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.history import PnlAnalyticsOut
from app.services.analytics_service import AnalyticsService

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get(
    "/pnl",
    response_model=PnlAnalyticsOut,
    status_code=status.HTTP_200_OK,
    summary="Get FIFO realised and unrealised PnL analytics",
)
async def get_pnl_analytics(
    symbol: str | None = Query(None, description="Optional symbol filter"),
    include_matches: bool = Query(False, description="Whether to include closed match lots"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PnlAnalyticsOut:
    """Compute FIFO realised PnL and live unrealised PnL for the authenticated user."""
    service = AnalyticsService(session)
    return await service.get_pnl_analytics(
        user_id=user.id,
        symbol=symbol,
        include_matches=include_matches,
    )

