"""TradeRepository — DB access for the trades table.

Trade records are **immutable by design**: once a trade is executed and
recorded, it is never updated or deleted.  FIFO PnL, analytics and audit
trails all depend on this immutability.  Accordingly, this repository
exposes insert and query methods only — no update, no delete.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trade import Trade, TradeType
from app.repositories.base import BaseRepository


class TradeRepository(BaseRepository[Trade]):
    """Insert and query repository for executed trades."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Trade, session)

    async def list_trades_keyset(
        self,
        user_id: UUID,
        symbol: str | None = None,
        trade_type: str | None = None,
        from_: datetime | None = None,
        to_: datetime | None = None,
        cursor_dt: datetime | None = None,
        cursor_id: UUID | None = None,
        limit: int = 20,
    ) -> list[Trade]:
        """Fetch trades for *user_id* using keyset pagination on (executed_at DESC, id DESC).

        Fetches *limit + 1* rows to determine if subsequent pages exist.
        """
        conditions = [Trade.user_id == user_id]

        if symbol:
            conditions.append(Trade.symbol == symbol.strip().upper())

        if trade_type:
            norm_type = trade_type.strip().upper()
            conditions.append(Trade.trade_type == TradeType(norm_type))

        if from_ is not None:
            conditions.append(Trade.executed_at >= from_)

        if to_ is not None:
            conditions.append(Trade.executed_at < to_)

        if cursor_dt is not None and cursor_id is not None:
            # Keyset condition for descending order:
            # executed_at < cursor_dt OR (executed_at == cursor_dt AND id < cursor_id)
            cursor_cond = or_(
                Trade.executed_at < cursor_dt,
                and_(
                    Trade.executed_at == cursor_dt,
                    Trade.id < cursor_id,
                ),
            )
            conditions.append(cursor_cond)

        stmt = (
            select(Trade)
            .where(and_(*conditions))
            .order_by(Trade.executed_at.desc(), Trade.id.desc())
            .limit(limit + 1)
        )

        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_trades(
        self,
        user_id: UUID,
        symbol: str | None = None,
        trade_type: str | None = None,
        from_: datetime | None = None,
        to_: datetime | None = None,
    ) -> int:
        """Count total matching trades for *user_id* without pagination constraints."""
        conditions = [Trade.user_id == user_id]

        if symbol:
            conditions.append(Trade.symbol == symbol.strip().upper())

        if trade_type:
            norm_type = trade_type.strip().upper()
            conditions.append(Trade.trade_type == TradeType(norm_type))

        if from_ is not None:
            conditions.append(Trade.executed_at >= from_)

        if to_ is not None:
            conditions.append(Trade.executed_at < to_)

        stmt = select(func.count(Trade.id)).where(and_(*conditions))
        result = await self._session.execute(stmt)
        return result.scalar() or 0

    async def list_chronological_trades(
        self,
        user_id: UUID,
        symbol: str | None = None,
    ) -> list[Trade]:
        """Fetch all trades for *user_id* sorted chronologically for FIFO PnL computation."""
        conditions = [Trade.user_id == user_id]

        if symbol:
            conditions.append(Trade.symbol == symbol.strip().upper())

        stmt = (
            select(Trade)
            .where(and_(*conditions))
            .order_by(Trade.executed_at.asc(), Trade.id.asc())
        )

        result = await self._session.execute(stmt)
        return list(result.scalars().all())
