"""Trade endpoints — POST /api/trade/buy and /api/trade/sell.

Thin router: validates the request, delegates to TradeService,
returns the result. No business logic lives here.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.trade import TradeRequest, TradeResult
from app.services.trade_service import TradeService

router = APIRouter(prefix="/api/trade", tags=["trade"])


@router.post("/buy", response_model=TradeResult, status_code=status.HTTP_201_CREATED)
async def buy(
    body: TradeRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> TradeResult:
    """Buy a cryptocurrency at the current live price."""
    service = TradeService(session)
    return await service.buy(user.id, body.symbol, body.quantity)


@router.post("/sell", response_model=TradeResult, status_code=status.HTTP_201_CREATED)
async def sell(
    body: TradeRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> TradeResult:
    """Sell a cryptocurrency at the current live price."""
    service = TradeService(session)
    return await service.sell(user.id, body.symbol, body.quantity)

