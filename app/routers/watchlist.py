"""Watchlist REST router: GET, POST, and DELETE /api/watchlist."""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.price_registry import get_price_status_safe
from app.models.user import User
from app.schemas.watchlist import WatchlistItemOut
from app.services.watchlist_service import WatchlistService
from app.services.ws_manager import ConnectionManager, get_ws_manager

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


@router.get(
    "",
    response_model=list[WatchlistItemOut],
    status_code=status.HTTP_200_OK,
    summary="Get personal watchlist with live prices",
)
async def get_watchlist(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> list[WatchlistItemOut]:
    """Retrieve all symbols on the authenticated user's watchlist with price status."""
    service = WatchlistService(session)
    return await service.list_with_prices(user.id)


@router.post(
    "/{symbol}",
    response_model=WatchlistItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add symbol to watchlist",
)
async def add_to_watchlist(
    symbol: str,
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    manager: ConnectionManager = Depends(get_ws_manager),
) -> WatchlistItemOut:
    """Add a symbol to the authenticated user's personal watchlist.

    Returns 201 Created if newly added, or 200 OK if already present (idempotent).
    """
    service = WatchlistService(session, manager=manager)
    item, created = await service.add(user.id, symbol)
    if not created:
        response.status_code = status.HTTP_200_OK

    price, age_ms, stale = get_price_status_safe(item.symbol)
    return WatchlistItemOut(
        symbol=item.symbol,
        added_at=item.added_at,
        price=price,
        price_age_ms=age_ms,
        stale=stale,
    )


@router.delete(
    "/{symbol}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove symbol from watchlist",
)
async def remove_from_watchlist(
    symbol: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    manager: ConnectionManager = Depends(get_ws_manager),
) -> Response:
    """Remove a symbol from the authenticated user's personal watchlist.

    Returns 204 No Content on success, or 404 NOT_IN_WATCHLIST if absent.
    """
    service = WatchlistService(session, manager=manager)
    await service.remove(user.id, symbol)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

