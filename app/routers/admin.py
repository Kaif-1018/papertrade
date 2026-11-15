"""Admin router exposing platform controls for trading pairs, portfolio resets, and metrics."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_admin
from app.core.database import async_session_factory
from app.models.user import User
from app.schemas.admin import (
    AdminPairCreate,
    AdminPairDeleteOut,
    AdminPairOut,
    AdminResetOut,
    AdminResetRequest,
    AdminStatsOut,
)
from app.services.admin_service import AdminService
from app.services.notifier import get_trade_notifier

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@router.post(
    "/pairs",
    response_model=AdminPairOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add or reactivate a trading pair",
)
async def add_trading_pair(
    payload: AdminPairCreate,
    request: Request,
    session: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_user),
) -> AdminPairOut:
    """Validate format, verify on Binance, persist in DB, and subscribe price feed."""
    service = AdminService(session)
    price_feed = getattr(request.app.state, "price_feed", None)
    pair, feed_sync = await service.add_pair(
        admin_id=current_admin.id,
        symbol=payload.symbol,
        price_feed=price_feed,
    )

    pairs = await service.list_pairs(price_feed=price_feed)
    matched = next((p for p in pairs if p.symbol == pair.symbol), None)
    if matched is not None:
        matched.feed_sync = feed_sync
        return matched

    return AdminPairOut(
        symbol=pair.symbol,
        is_active=pair.is_active,
        holder_count=0,
        last_price=None,
        price_age_ms=None,
        feed_subscribed=False,
        added_at=pair.added_at,
        feed_sync=feed_sync,
    )


@router.delete(
    "/pairs/{symbol}",
    response_model=AdminPairDeleteOut,
    summary="Soft-deactivate a trading pair",
)
async def deactivate_trading_pair(
    symbol: str,
    request: Request,
    force: bool = Query(False, description="Force deactivation even if users hold the symbol"),
    session: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_user),
) -> AdminPairDeleteOut:
    """Soft-deactivate a trading pair and unsubscribe from the live price feed."""
    service = AdminService(session)
    price_feed = getattr(request.app.state, "price_feed", None)
    sym, is_active, feed_sync = await service.deactivate_pair(
        admin_id=current_admin.id,
        symbol=symbol,
        force=force,
        price_feed=price_feed,
    )
    return AdminPairDeleteOut(
        symbol=sym,
        is_active=is_active,
        feed_sync=feed_sync,
    )


@router.get(
    "/pairs",
    response_model=list[AdminPairOut],
    summary="List all trading pairs with status and metrics",
)
async def list_trading_pairs(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> list[AdminPairOut]:
    """Retrieve all trading pairs with active status, holder counts, and feed subscription state."""
    service = AdminService(session)
    price_feed = getattr(request.app.state, "price_feed", None)
    return await service.list_pairs(price_feed=price_feed)


@router.post(
    "/users/{user_id}/reset",
    response_model=AdminResetOut,
    summary="Reset a user's portfolio to initial balance",
)
async def reset_user_portfolio(
    user_id: UUID,
    payload: AdminResetRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_user),
) -> AdminResetOut:
    """Atomically wipe user holdings, restore initial balance, and log audit trail."""
    service = AdminService(session)
    notifier = getattr(request.app.state, "trade_notifier", None)
    return await service.reset_portfolio(
        admin_id=current_admin.id,
        user_id=user_id,
        reason=payload.reason,
        notifier=notifier,
    )


@router.get(
    "/stats",
    response_model=AdminStatsOut,
    summary="Retrieve platform operational and financial statistics",
)
async def get_platform_stats(
    request: Request,
) -> AdminStatsOut:
    """Calculate platform aggregates across users, trades, connections, and feed health."""
    ws_manager = getattr(request.app.state, "ws_manager", None)
    price_feed = getattr(request.app.state, "price_feed", None)
    session_factory_to_use = getattr(
        request.app.state, "session_factory", async_session_factory
    )
    return await AdminService.get_platform_stats(
        session_factory=session_factory_to_use,
        ws_manager=ws_manager,
        price_feed=price_feed,
    )
