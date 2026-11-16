"""Portfolio valuation REST endpoint — GET /api/portfolio."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.rate_limit import rate_limit
from app.models.user import User
from app.schemas.portfolio import PortfolioOut
from app.services.portfolio_service import PortfolioService

router = APIRouter(
    prefix="/api/portfolio",
    tags=["portfolio"],
    dependencies=[Depends(rate_limit("general_api", limit=120, window_seconds=60, key="user"))],
)


@router.get("", response_model=PortfolioOut)
async def get_portfolio(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PortfolioOut:
    """Retrieve the current user's portfolio valuation and unrealised PnL.

    Read-only query: no database row locks are acquired. Prices are retrieved
    safely from the in-memory registry.
    """
    service = PortfolioService(session)
    return await service.get_portfolio(current_user.id)

