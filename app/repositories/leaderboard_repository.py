"""Leaderboard repository for efficient, bulk data retrieval without N+1 queries."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.holding import Holding
from app.models.user import User


@dataclass(frozen=True)
class UserLeaderboardRow:
    """Lightweight projection of User columns needed for leaderboard computation."""

    id: UUID
    email: str
    virtual_balance: Decimal
    initial_balance: Decimal
    created_at: datetime


@dataclass(frozen=True)
class HoldingLeaderboardRow:
    """Lightweight projection of Holding columns needed for portfolio valuation."""

    user_id: UUID
    symbol: str
    quantity: Decimal
    avg_buy_price: Decimal


class LeaderboardRepository:
    """Read-only bulk extractor for platform-wide user balances and portfolio holdings.

    Guarantees constant O(1) database queries (exactly 2 queries) regardless of
    user base size, eliminating all N+1 query patterns.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all_users_and_holdings(
        self,
    ) -> tuple[list[UserLeaderboardRow], list[HoldingLeaderboardRow]]:
        """Fetch all user balances and holdings using strictly two SQL statements.

        Returns:
            Tuple of (users_list, holdings_list).
        """
        # Query 1: All users (id, email, virtual_balance, initial_balance, created_at)
        user_stmt = select(
            User.id,
            User.email,
            User.virtual_balance,
            User.initial_balance,
            User.created_at,
        )
        user_res = await self._session.execute(user_stmt)
        users = [
            UserLeaderboardRow(
                id=row.id,
                email=row.email,
                virtual_balance=row.virtual_balance,
                initial_balance=row.initial_balance,
                created_at=row.created_at,
            )
            for row in user_res.all()
        ]

        # Query 2: All open holdings (user_id, symbol, quantity, avg_buy_price)
        holding_stmt = select(
            Holding.user_id,
            Holding.symbol,
            Holding.quantity,
            Holding.avg_buy_price,
        ).where(Holding.quantity > Decimal("0"))
        holding_res = await self._session.execute(holding_stmt)
        holdings = [
            HoldingLeaderboardRow(
                user_id=row.user_id,
                symbol=row.symbol,
                quantity=row.quantity,
                avg_buy_price=row.avg_buy_price,
            )
            for row in holding_res.all()
        ]

        return users, holdings
