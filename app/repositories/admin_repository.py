"""Repository for administrative operations, audit tracking, and platform statistics."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.holding import Holding
from app.models.portfolio_reset import PortfolioReset
from app.models.trade import Trade
from app.models.user import User


class AdminRepository:
    """Database queries supporting administrative controls and aggregations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_portfolio_reset(
        self,
        user_id: UUID,
        admin_id: UUID | None,
        reason: str,
        balance_before: Decimal,
        holdings_snapshot: list[dict[str, Any]],
    ) -> PortfolioReset:
        """Stage an audit row for a portfolio reset."""
        reset_record = PortfolioReset(
            user_id=user_id,
            admin_id=admin_id,
            reason=reason,
            balance_before=balance_before,
            holdings_snapshot=holdings_snapshot,
        )
        self._session.add(reset_record)
        await self._session.flush()
        return reset_record

    async def get_latest_reset(self, user_id: UUID) -> PortfolioReset | None:
        """Fetch the most recent portfolio reset for a user."""
        stmt = (
            select(PortfolioReset)
            .where(PortfolioReset.user_id == user_id)
            .order_by(PortfolioReset.reset_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_holders_count_for_symbol(self, symbol: str) -> int:
        """Return the number of distinct users who currently hold open positions for a symbol."""
        stmt = (
            select(func.count(func.distinct(Holding.user_id)))
            .where(
                Holding.symbol == symbol.strip().upper(),
                Holding.quantity > 0,
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one() or 0

    async def get_all_symbols_holder_counts(self) -> dict[str, int]:
        """Return a mapping of symbol to number of distinct users with quantity > 0."""
        stmt = (
            select(
                Holding.symbol,
                func.count(func.distinct(Holding.user_id)),
            )
            .where(Holding.quantity > 0)
            .group_by(Holding.symbol)
        )
        result = await self._session.execute(stmt)
        return {row[0]: row[1] for row in result.all()}

    # ------------------------------------------------------------------
    # Concurrently executable SQL aggregations for platform stats
    # ------------------------------------------------------------------

    @staticmethod
    async def get_total_users(session: AsyncSession) -> int:
        """Execute aggregate query counting total registered users."""
        stmt = select(func.count(User.id))
        result = await session.execute(stmt)
        return result.scalar_one() or 0

    @staticmethod
    async def get_trade_volume_stats(
        session: AsyncSession,
        since_24h: datetime,
    ) -> dict[str, Any]:
        """Execute aggregate query for 24h and all-time trade volumes and counts."""
        stmt = select(
            func.count(Trade.id).label("total_trades"),
            func.coalesce(func.sum(Trade.total_value), Decimal("0")).label("volume_all_time"),
            func.count(case((Trade.executed_at >= since_24h, Trade.id))).label("trades_24h"),
            func.coalesce(
                func.sum(case((Trade.executed_at >= since_24h, Trade.total_value), else_=Decimal("0"))),
                Decimal("0"),
            ).label("volume_24h"),
            func.count(func.distinct(case((Trade.executed_at >= since_24h, Trade.user_id)))).label(
                "active_traders_24h"
            ),
        )
        result = await session.execute(stmt)
        row = result.one()
        return {
            "total_trades": row.total_trades,
            "volume_all_time": row.volume_all_time,
            "trades_24h": row.trades_24h,
            "volume_24h": row.volume_24h,
            "active_traders_24h": row.active_traders_24h,
        }

    @staticmethod
    async def get_top_symbols_by_volume(
        session: AsyncSession,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Execute aggregate query finding top symbols by all-time traded volume."""
        stmt = (
            select(
                Trade.symbol,
                func.coalesce(func.sum(Trade.total_value), Decimal("0")).label("volume"),
                func.count(Trade.id).label("trade_count"),
            )
            .group_by(Trade.symbol)
            .order_by(func.sum(Trade.total_value).desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return [
            {
                "symbol": row.symbol,
                "volume": row.volume,
                "trade_count": row.trade_count,
            }
            for row in result.all()
        ]
