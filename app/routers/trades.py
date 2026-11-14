"""Trades history router: GET /api/trades."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.history import TradeHistoryOut
from app.services.analytics_service import AnalyticsService

router = APIRouter(prefix="/api/trades", tags=["trades"])


@router.get(
    "",
    response_model=TradeHistoryOut,
    status_code=status.HTTP_200_OK,
    summary="Get paginated trade history",
)
async def get_trade_history(
    symbol: str | None = Query(None, description="Filter by trading pair symbol"),
    type: str | None = Query(None, description="Filter by trade direction (BUY or SELL)"),
    from_: datetime | None = Query(None, alias="from", description="Inclusive start datetime"),
    to_: datetime | None = Query(None, alias="to", description="Exclusive end datetime"),
    limit: int = Query(20, ge=1, le=100, description="Page size (max 100)"),
    cursor: str | None = Query(None, description="Opaque keyset cursor from prior response"),
    include_total: bool = Query(False, description="Whether to include full total matching count"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> TradeHistoryOut:
    """Retrieve the authenticated caller's trade history with keyset pagination."""
    service = AnalyticsService(session)
    return await service.get_trade_history(
        user_id=user.id,
        symbol=symbol,
        trade_type=type,
        from_=from_,
        to_=to_,
        limit=limit,
        cursor=cursor,
        include_total=include_total,
    )

